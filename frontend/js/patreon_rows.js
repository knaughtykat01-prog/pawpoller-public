/* Patreon's options for one piece (spec 084): who can see it, which renders go in, the cover, the collection and
 * "Tell my members". One panel shared by the artwork form, the story editor and the account defaults, so the rules
 * live in one place. The tiers and collections are read from Patreon only when the panel is opened.
 *
 * Saved into the piece (artwork categories.pat / story platform_options.pat) as the keys the server reads
 * (posting/pat_choices.py CHOICE_KEYS): audience, tier_ids, notify, cover, variant_keys, files, collection,
 * picture_place, caption, publish_at, public_at. Times are saved as UTC ISO and shown in the operator's zone.
 * The two dates are offered on art only: a story's options are shared by every chapter sent with them. Everything is named by key; the server turns
 * keys into files itself. The server also re-checks every rule below at send time, so this panel is help, not a lock.
 */
window.PatreonRows = (function () {
    const esc = (s) => Utils.escapeHtml(String(s == null ? '' : s));
    // The server's two word lists (posting/platforms/base.py _GENERAL_WORDS / _MATURE_WORDS): change both together.
    const RANK = { 'general': 0, 'general audiences': 0, 'g': 0, 'safe': 0, 'sfw': 0, 'clean': 0,
                   'teen': 1, 'teen and up audiences': 1, 't': 1, 'mature': 1, 'm': 1, 'questionable': 1, 'q': 1 };
    const rank = (r) => { const k = String(r || '').trim().toLowerCase(); return k in RANK ? RANK[k] : 2; };
    const money = (cents, cur) => (cents / 100).toLocaleString(undefined, { style: 'currency', currency: cur || 'USD' });
    // Picker values are in the operator's zone (Utils.time); saved as UTC with a "Z".
    const toLocal = (iso) => (iso ? Utils.time.toPicker(iso) || '' : '');
    const toUtc = (v) => { const d = Utils.time.toUtc(v); return d ? d.toISOString().replace(/\.\d{3}Z$/, 'Z') : ''; };

    /* ctx: { kind: 'art' | 'story' | 'defaults', rating, renders: [{key, label, rating}], files: {pdf, epub},
     *        storyCover: bool } */
    function html(saved, ctx) {
        saved = saved || {};
        ctx = ctx || {};
        if (ctx.kind === 'art' && ctx.rating === undefined) {      // the new-artwork form: no saved piece yet
            return `<div class="field pat-rows" data-off="1"><span>Patreon</span>
                <small style="color:var(--text-muted)">Save the piece first, then choose who sees it on Patreon here. Until then your Patreon defaults (Accounts) apply.</small></div>`;
        }
        if (ctx.kind !== 'defaults' && rank(ctx.rating) > 1) {
            return `<div class="field pat-rows" data-off="1"><span>Patreon</span>
                <small style="color:var(--text-muted)">Patreon only takes General and Mature work from PawPoller. Rate this piece General or Mature to post it there.</small></div>`;
        }
        const mature = rank(ctx.rating) === 1;
        const renders = (ctx.renders || []).filter(r => r && r.key && rank(r.rating) <= rank(ctx.rating));
        const picked = new Set((saved.variant_keys || []).map(String));
        const cover = String(saved.cover || 'none');
        // The main picture and the story's cover carry the piece's own rating, and only a General cover is sent.
        const coverOpts = ctx.kind === 'story'
            ? [['none', 'No cover'], ...(ctx.storyCover && !mature ? [['story', "The story's cover"]] : [])]
            : [['none', 'No cover'], ...(mature ? [] : [['main', 'The main picture']]),
               ...renders.filter(r => rank(r.rating) === 0).map(r => ['variant:' + r.key, r.label || r.key])];
        const files = ctx.files || {};
        const pickedFiles = new Set((saved.files || []).map(String));
        return `<div class="field pat-rows" data-saved="${esc(JSON.stringify(saved))}" data-mature="${mature ? 1 : 0}">
            <span>Who can see this on Patreon${window.platformByCode && (window.platformByCode('pat') || {}).testing ? ' <span class="card-type-badge" title="Not yet proven on a real post">being tested</span>' : ''}</span>
            <select class="pat-audience" aria-label="Who can see this on Patreon">
                ${ctx.kind === 'defaults' ? '' : `<option value=""${saved.audience ? '' : ' selected'}>My Patreon default (Accounts)</option>`}
                ${mature ? '' : `<option value="public"${saved.audience === 'public' ? ' selected' : ''}>Everyone (public)</option>
                <option value="patrons"${saved.audience === 'patrons' ? ' selected' : ''}>All paying members</option>`}
                <option value="tiers"${saved.audience === 'tiers' || (ctx.kind === 'defaults' && !saved.audience) || (mature && saved.audience && saved.audience !== 'tiers') ? ' selected' : ''}>Only these tiers…</option>
            </select>
            ${mature ? '<small style="color:var(--text-muted)">Mature work can only go to paid tiers on Patreon.</small>' : ''}
            <div class="pat-tiers" style="font-size:13px;color:var(--text-muted)">Tiers load when this is opened…</div>
            ${ctx.kind === 'defaults' ? '' : `
            ${renders.length && ctx.kind === 'art' ? `<span>Also include these pictures (in this order)</span>
                <div>${renders.map(r => `<label style="display:flex;gap:8px;align-items:center"><input type="checkbox" class="pat-render" style="width:auto" value="${esc(r.key)}"${picked.has(r.key) ? ' checked' : ''}> ${esc(r.label || r.key)}</label>`).join('')}</div>` : ''}
            <label>Cover <select class="pat-cover">${coverOpts.map(([v, l]) => `<option value="${esc(v)}"${cover === v ? ' selected' : ''}>${esc(l)}</option>`).join('')}</select></label>
            <small style="color:var(--text-muted)">The title, tags and cover, and maybe the picture's caption and alt text, are public on Patreon whatever the tier. Pick a cover without nudity; only a General cover is shown unblurred.</small>
            ${ctx.kind === 'story' ? `<div>${['pdf', 'epub'].map(f => `<label style="display:flex;gap:8px;align-items:center"><input type="checkbox" class="pat-file" style="width:auto" value="${f}"${pickedFiles.has(f) ? ' checked' : ''}${files[f] ? '' : ' disabled'}> Attach the whole story as ${f.toUpperCase()}${files[f] ? '' : ' (no file)'}</label>`).join('')}</div>` : ''}
            <label>Collection <select class="pat-collection"><option value="none">None</option>
                ${ctx.kind === 'story' ? `<option value="story"${saved.collection === 'story' || saved.collection === undefined ? ' selected' : ''}>This story's collection (public, named after the story)</option>` : ''}
                ${saved.collection && !['none', 'story'].includes(String(saved.collection)) ? `<option value="${esc(saved.collection)}" selected>Saved collection</option>` : ''}
            </select></label>`}
            <label style="display:flex;gap:8px;align-items:center"><input type="checkbox" class="pat-notify" style="width:auto"${saved.notify === false ? '' : ' checked'}> Tell my members (Patreon emails and notifies them)</label>
            ${ctx.kind === 'defaults' ? '' : `
            ${ctx.kind === 'art' ? `<label>Pictures go in <select class="pat-place">
                    <option value="gallery"${saved.picture_place === 'text' && rank(ctx.rating) === 0 ? '' : ' selected'}>The gallery at the top</option>
                    ${rank(ctx.rating) === 0 ? `<option value="text"${saved.picture_place === 'text' ? ' selected' : ''}>The post's text, below the members-only line</option>` : ''}</select></label>
                <label>Caption under the main picture <input type="text" class="pat-caption" maxlength="140" value="${esc(saved.caption || '')}" placeholder="(optional, up to 140 characters)"></label>` : ''}
            ${rank(ctx.rating) === 0 && ctx.kind === 'art' ? `<label>Let Patreon publish it at <input type="datetime-local" class="pat-publish-at" value="${esc(toLocal(saved.publish_at))}"></label>
                <small style="color:var(--text-muted)">Optional, General work only. Patreon publishes it then, even if PawPoller isn't running.</small>
                <label>Make it public on <input type="datetime-local" class="pat-public-at" value="${esc(toLocal(saved.public_at))}"></label>
                <small style="color:var(--text-muted)">Optional, General work only. Your members see it first; from this date everyone can.</small>
                <small style="color:var(--text-muted)">Once either date is set, Patreon holds it: PawPoller can't stop or change it afterwards, even if you change the piece. Use Patreon for that.</small>` : ''}`}
        </div>`;
    }

    /* Fill the tiers and collections once the panel is opened (or at once when it isn't inside a <details>). */
    function wire(root, accountId) {
        const box = root && root.querySelector('.pat-rows:not([data-off])');
        if (!box) return;
        const det = box.closest('details');
        const load = async () => {
            box.dataset.opened = '1';      // from here on collect() returns the panel's choices
            if (box.dataset.loaded) return;
            box.dataset.loaded = '1';
            let saved = {};
            try { saved = JSON.parse(box.dataset.saved || '{}'); } catch (e) { saved = {}; }
            const chosen = new Set((saved.tier_ids || []).map(String));
            const mature = box.dataset.mature === '1';
            const list = box.querySelector('.pat-tiers');
            const id = typeof accountId === 'function' ? accountId() : accountId;
            try {
                const r = await API.patTiers(id || undefined);
                const tiers = (r.rules || []).filter(t => t.kind === 'tier' && (!mature || !t.free));
                list.style.color = '';
                list.innerHTML = tiers.length ? `
                    ${mature ? '<button type="button" class="btn btn-sm pat-all-paid">All paid tiers</button>' : ''}
                    ${tiers.map(t => `<label style="display:flex;gap:8px;align-items:center"><input type="checkbox" class="pat-tier" style="width:auto" value="${esc(t.tier_id)}"${chosen.has(String(t.tier_id)) ? ' checked' : ''}>
                        ${esc(t.title)} · ${t.free ? 'free' : esc(money(t.amount_cents, r.currency))}</label>`).join('')}`
                    : (mature ? 'This page has no paid tiers, so Mature work can\'t go up there yet.' : 'This page has no tiers.');
                const all = list.querySelector('.pat-all-paid');
                if (all) all.addEventListener('click', () => list.querySelectorAll('.pat-tier').forEach(c => { c.checked = true; }));
                box.dataset.ready = '1';
            } catch (e) {
                delete box.dataset.loaded;
                list.textContent = 'Could not load your Patreon tiers (' + (e.message || 'sign in to Patreon') + '). Close and open this again to retry.';
            }
            const sel = box.querySelector('.pat-collection');
            if (sel) {
                try {
                    const c = await API.patCollections(id || undefined);
                    const keep = sel.value;
                    sel.querySelectorAll('option[data-c], option[value]:not([value="none"]):not([value="story"])').forEach(o => o.remove());
                    (c.collections || []).forEach(col => {
                        const o = document.createElement('option');
                        o.value = col.id; o.dataset.c = '1'; o.textContent = col.title || ('Collection ' + col.id);
                        if (String(keep) === String(col.id) || String(saved.collection) === String(col.id)) o.selected = true;
                        sel.appendChild(o);
                    });
                } catch (e) { /* the saved choice stays; the server checks it at send time */ }
            }
        };
        if (det) det.addEventListener('toggle', () => { if (det.open) load(); });
        else load();
    }

    /* The seven keys, or null when the panel isn't there (or was never opened, so nothing changed). */
    function collect(root) {
        const box = root && root.querySelector('.pat-rows:not([data-off])');
        if (!box || !box.dataset.opened) return null;    // never opened: keep what was saved (or the account default)
        let saved = {};
        try { saved = JSON.parse(box.dataset.saved || '{}'); } catch (e) { saved = {}; }
        const out = { ...saved };
        out.audience = box.querySelector('.pat-audience').value;
        if (box.dataset.ready) out.tier_ids = Array.from(box.querySelectorAll('.pat-tier:checked')).map(c => c.value);
        if (out.audience !== 'tiers') out.tier_ids = [];
        if (!out.audience) { delete out.audience; delete out.tier_ids; }   // the account default fills in at send time
        out.notify = box.querySelector('.pat-notify').checked;
        const cover = box.querySelector('.pat-cover');
        if (cover) out.cover = cover.value;
        const renders = box.querySelectorAll('.pat-render');
        if (renders.length) out.variant_keys = Array.from(renders).filter(c => c.checked).map(c => c.value);
        const files = box.querySelectorAll('.pat-file');
        if (files.length) out.files = Array.from(files).filter(c => c.checked).map(c => c.value);
        const col = box.querySelector('.pat-collection');
        if (col) out.collection = col.value;
        const place = box.querySelector('.pat-place');
        if (place) out.picture_place = place.value;
        const cap = box.querySelector('.pat-caption');
        if (cap) out.caption = cap.value.trim();
        const pub = box.querySelector('.pat-publish-at');
        out.publish_at = pub ? toUtc(pub.value) : '';    // a Mature piece never keeps a Patreon-side time
        const vis = box.querySelector('.pat-public-at');
        out.public_at = vis ? toUtc(vis.value) : '';     // a Mature piece never keeps an early-access date
        const keys = ['audience', 'tier_ids', 'notify', 'cover', 'variant_keys', 'files', 'collection',
                      'picture_place', 'caption', 'publish_at', 'public_at'];
        return Object.fromEntries(keys.filter(k => k in out).map(k => [k, out[k]]));
    }

    return { html, wire, collect, rank };
})();
