/* ── Podcasts (MEDIAPLATS §4, 4.21.1) ────────────────────────────────────────
 *
 * Feeds PawPoller serves itself: make a feed, publish audio pieces as episodes,
 * copy the public feed URL into the directories, see which crawler last read it.
 * Publishing an episode from here is the same row the publish pickers write
 * through the `pod` poster; this page is the place to manage the feed itself.
 * ───────────────────────────────────────────────────────────────────────── */
const Podcasts = {
    _data: null,
    _feed: null,

    _root() {
        // The main content area is <main id="app"> (the same node App._setContent replaces).
        return document.getElementById('app') || document.querySelector('main') || document.body;
    },
    esc(s) { return String(s == null ? '' : s).replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c])); },

    async render(feedId) {
        const root = this._root();
        root.innerHTML = '<div class="muted" style="padding:2rem;">Loading podcasts…</div>';
        try {
            this._data = await API.getPodcasts();
        } catch (e) {
            root.innerHTML = `<div class="error" style="padding:2rem;">Could not load podcasts: ${this.esc(e.message || e)}</div>`;
            return;
        }
        if (feedId) return this._renderFeed(root, feedId);
        this._renderList(root);
    },

    _renderList(root) {
        const d = this._data;
        const noBase = !d.public_base;
        const feeds = d.feeds.map(f => `
            <a class="story-card" href="#/podcasts/${f.feed_id}" style="display:block;">
                <div class="story-card-body">
                    <div class="story-card-title">${this.esc(f.title)}</div>
                    <div class="muted" style="font-size:.85rem;">/feed/${this.esc(f.slug)}.xml · ${f.episode_count} episode${f.episode_count === 1 ? '' : 's'}${f.explicit ? ' · explicit' : ''}</div>
                </div>
            </a>`).join('');
        root.innerHTML = `
            <div class="page-header">
                <div><div class="eyebrow">Your works</div><h1>Podcasts</h1>
                <p class="muted">A feed PawPoller serves itself. Submit its address once to each directory — Apple Podcasts, Spotify, Amazon Music, Pocket Casts — and every audio piece you publish to it becomes an episode wherever people listen.</p></div>
            </div>
            ${noBase ? `<div class="card" style="border-color:var(--warning,#c9a227);margin-bottom:1rem;"><strong>This install has no public address.</strong> A feed has to be fetched from the internet — set <code>IG_PUBLIC_BASE_URL</code> on the server (the same setting the Instagram host uses). You can make and fill feeds now; directories can read them once the address is set.</div>` : ''}
            <div class="card" style="margin-bottom:1rem;">
                <h3 style="margin-top:0;">New feed</h3>
                <div style="display:grid;grid-template-columns:1fr 1fr;gap:.6rem;max-width:720px;">
                    <label>Title<input id="pod-title" type="text" placeholder="Sample Show"></label>
                    <label>Address<input id="pod-slug" type="text" placeholder="sample-show (letters, digits, hyphens)"></label>
                    <label>Author<input id="pod-author" type="text" placeholder="Shown in players"></label>
                    <label>Owner email<input id="pod-email" type="email" placeholder="Directories require one; never shown publicly except in the feed"></label>
                    <label>Category<select id="pod-category">${d.categories.map(c => `<option${c === 'Arts' ? ' selected' : ''}>${this.esc(c)}</option>`).join('')}</select></label>
                    <label>Language<input id="pod-language" type="text" value="en"></label>
                    <label style="grid-column:1/-1;">Description<textarea id="pod-description" rows="3"></textarea></label>
                    <label><input id="pod-explicit" type="checkbox"> Explicit feed</label>
                </div>
                <div style="margin-top:.6rem;"><button class="btn btn-primary" id="pod-create">Create feed</button> <span id="pod-msg" class="muted"></span></div>
            </div>
            <div class="story-card-grid">${feeds || '<p class="muted">No feeds yet.</p>'}</div>`;
        document.getElementById('pod-title').addEventListener('input', e => {
            const s = document.getElementById('pod-slug');
            if (!s.dataset.touched) s.value = e.target.value.toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-+|-+$/g, '').slice(0, 64);
        });
        document.getElementById('pod-slug').addEventListener('input', e => { e.target.dataset.touched = '1'; });
        document.getElementById('pod-create').addEventListener('click', () => this._create());
    },

    async _create() {
        const msg = document.getElementById('pod-msg');
        const body = {
            title: document.getElementById('pod-title').value.trim(),
            slug: document.getElementById('pod-slug').value.trim(),
            author: document.getElementById('pod-author').value.trim(),
            owner_email: document.getElementById('pod-email').value.trim(),
            category: document.getElementById('pod-category').value,
            language: document.getElementById('pod-language').value.trim() || 'en',
            description: document.getElementById('pod-description').value,
            explicit: document.getElementById('pod-explicit').checked,
        };
        if (!body.title) { msg.textContent = 'A feed needs a title.'; return; }
        msg.textContent = 'Creating…';
        try {
            const r = await API.createPodcast(body);
            location.hash = `#/podcasts/${r.feed_id}`;
        } catch (e) {
            msg.textContent = e.message || String(e);
        }
    },

    async _renderFeed(root, feedId) {
        let d;
        try { d = await API.getPodcast(feedId); }
        catch (e) { root.innerHTML = `<div class="error" style="padding:2rem;">${this.esc(e.message || e)}</div>`; return; }
        this._feed = d;
        const f = d.feed;
        const fetches = Object.entries(d.fetches || {}).map(([name, v]) => `<li>${this.esc(name)} — last ${this.esc(v.last_at || '')} (${v.count}×)</li>`).join('');
        const dirs = (this._data.directories || []).map(x => `<li><a href="${this.esc(x.url)}" target="_blank" rel="noopener">${this.esc(x.name)}</a> <span class="muted">— ${this.esc(x.note)}</span></li>`).join('');
        const eps = d.episodes.map(e => `
            <tr data-ep="${e.episode_id}">
                <td>${this.esc(e.title)}<div class="muted" style="font-size:.8rem;">${this.esc(e.artwork_name)}${e.piece_ok ? '' : ' · <span style="color:var(--danger,#b33)">piece missing</span>'}</div></td>
                <td>${e.duration_s ? (window.MediaKinds ? MediaKinds.fmtDuration(e.duration_s) : Math.round(e.duration_s) + ' s') : ''}</td>
                <td>${this.esc((e.published_at || '').slice(0, 10))}</td>
                <td>${e.explicit ? 'explicit' : ''}</td>
                <td>${e.page_url ? `<a href="${this.esc(e.page_url)}" target="_blank" rel="noopener">page</a> · ` : ''}<a href="#/artwork/image/${encodeURIComponent(e.artwork_name)}">piece</a> · <button class="btn btn-sm" data-unpublish="${e.episode_id}">Unpublish</button></td>
            </tr>`).join('');
        root.innerHTML = `
            <div class="page-header">
                <div><a href="#/podcasts" class="muted">← Podcasts</a><h1>${this.esc(f.title)}</h1>
                <p class="muted">${this.esc(f.description || '')}</p></div>
            </div>
            <div class="card" style="margin-bottom:1rem;">
                <h3 style="margin-top:0;">Feed address</h3>
                ${f.feed_url ? `<code id="pod-url">${this.esc(f.feed_url)}</code> <button class="btn btn-sm" id="pod-copy">Copy</button>`
                             : '<span class="muted">No public address yet — set <code>IG_PUBLIC_BASE_URL</code> on the server.</span>'}
                <h4>Submit it once to each directory</h4><ul>${dirs}</ul>
                <h4>Who has read it</h4>${fetches ? `<ul>${fetches}</ul>` : '<p class="muted">No directory has fetched this feed yet — RSS has no listener counts; this is the only truth available.</p>'}
            </div>
            <div class="card" style="margin-bottom:1rem;">
                <h3 style="margin-top:0;">Feed art</h3>
                ${f.art_url ? `<img src="${this.esc(f.art_url)}?t=${Date.now()}" alt="" style="width:160px;height:160px;border-radius:10px;object-fit:cover;">${f.art_from_episode ? '<p class="muted">Borrowed from the newest episode until you upload art of the feed\'s own.</p>' : ''}` : '<p class="muted">Directories want a square picture, 1400–3000 px. Upload one, or the feed uses a piece\'s poster.</p>'}
                <div style="margin-top:.5rem;"><input type="file" id="pod-art" accept="image/png,image/jpeg"> <button class="btn btn-sm" id="pod-art-up">Upload</button> <span id="pod-art-msg" class="muted"></span></div>
            </div>
            <div class="card" style="margin-bottom:1rem;">
                <h3 style="margin-top:0;">Episodes</h3>
                <p class="muted">Publish an audio piece here, or tick this feed in a piece's publish picker. Unpublishing removes the episode from the feed; the piece stays in the Library.</p>
                <div style="display:flex;gap:.5rem;align-items:center;flex-wrap:wrap;margin-bottom:.6rem;">
                    <input id="pod-ep-name" type="text" placeholder="Piece name (as in the Library)" style="min-width:260px;">
                    <input id="pod-ep-title" type="text" placeholder="Episode title (defaults to the piece's)">
                    <label><input id="pod-ep-explicit" type="checkbox"> explicit</label>
                    <button class="btn btn-primary btn-sm" id="pod-ep-add">Publish episode</button>
                    <span id="pod-ep-msg" class="muted"></span>
                </div>
                <table class="data-table"><thead><tr><th>Episode</th><th>Length</th><th>Published</th><th></th><th></th></tr></thead>
                <tbody>${eps || '<tr><td colspan="5" class="muted">No episodes yet.</td></tr>'}</tbody></table>
            </div>
            <div class="card">
                <button class="btn btn-danger btn-sm" id="pod-delete">Delete this feed</button>
                <span class="muted" style="font-size:.85rem;"> — removes the feed and its episode list. Pieces are never deleted. Directories that have it will show it as gone.</span>
            </div>`;
        const copy = document.getElementById('pod-copy');
        if (copy) copy.addEventListener('click', () => { navigator.clipboard.writeText(f.feed_url).then(() => { copy.textContent = 'Copied'; }); });
        document.getElementById('pod-art-up').addEventListener('click', async () => {
            const inp = document.getElementById('pod-art'), msg = document.getElementById('pod-art-msg');
            if (!inp.files || !inp.files[0]) { msg.textContent = 'Choose a PNG or JPEG.'; return; }
            msg.textContent = 'Uploading…';
            try { await API.uploadPodcastArt(f.feed_id, inp.files[0]); this._renderFeed(root, feedId); }
            catch (e) { msg.textContent = e.message || String(e); }
        });
        document.getElementById('pod-ep-add').addEventListener('click', async () => {
            const msg = document.getElementById('pod-ep-msg');
            const name = document.getElementById('pod-ep-name').value.trim();
            if (!name) { msg.textContent = 'Which piece?'; return; }
            msg.textContent = 'Publishing…';
            try {
                await API.addPodcastEpisode(f.feed_id, { artwork_name: name, title: document.getElementById('pod-ep-title').value.trim() || null,
                                                          explicit: document.getElementById('pod-ep-explicit').checked });
                this._renderFeed(root, feedId);
            } catch (e) { msg.textContent = e.message || String(e); }
        });
        root.querySelectorAll('[data-unpublish]').forEach(b => b.addEventListener('click', async () => {
            if (!confirm('Remove this episode from the feed? The piece stays in the Library.')) return;
            await API.removePodcastEpisode(b.dataset.unpublish);
            this._renderFeed(root, feedId);
        }));
        document.getElementById('pod-delete').addEventListener('click', async () => {
            if (!confirm(`Delete the feed "${f.title}"? Its episode list goes; the pieces stay.`)) return;
            await API.deletePodcast(f.feed_id);
            location.hash = '#/podcasts';
        });
    },
};

if (typeof window !== 'undefined') window.Podcasts = Podcasts;
