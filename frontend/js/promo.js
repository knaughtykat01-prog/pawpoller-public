/* ── Promo Maker (#/promo) ──────────────────────────────────────────────────
 *
 * A client-side "BookTok"-style promotional-image generator: paste an excerpt,
 * highlight the spicy phrases in colour, style words bold / italic / underlined
 * / struck, drop it on a background, and export a social-ready PNG (square /
 * portrait / story / any custom size). Everything runs in-browser on a
 * <canvas> — no server round-trip; the serif face (Lora, OFL) ships with the
 * app so the card renders identically on the desktop app, a phone and the
 * server. Modelled on the viral book-excerpt cards (a serif page with pastel
 * highlights).
 *
 * Data model (v2, 4.15.0 — docs/specs/promo_maker_v2.md §1): the excerpt lives
 * in a <textarea>; `highlights` are { start, end, color | censor } character
 * ranges that never overlap; `styles` are { start, end, b, i, u, s } ranges
 * that may overlap (flags OR together). Layout works on *segments*: a word is
 * split wherever a range boundary falls inside it, so "un[believ]able" carries
 * the highlight on the middle letters only, and a highlight paints as ONE
 * continuous run across the words (and the gaps between them) it covers on a
 * line — not a rect per word.
 *
 * The pure helpers (tokenise / segment / layout / runs / style toggling) take a
 * canvas context only for `measureText`, so `tests/test_promo_logic.py` drives
 * this file through node with a stub context.
 * ───────────────────────────────────────────────────────────────────────── */
const Promo = {

    // Canvas size presets (social aspect ratios). The canvas IS the export, so
    // these are real pixel dimensions. 'custom' takes any W×H within the clamp.
    SIZES: {
        square: { w: 1080, h: 1080, label: 'Square 1:1' },
        portrait: { w: 1080, h: 1350, label: 'Portrait 4:5' },
        story: { w: 1080, h: 1920, label: 'Story 9:16' },
        custom: { w: 1080, h: 1350, label: 'Custom…' },
    },
    MIN_SIDE: 320,
    MAX_SIDE: 4096,

    // Highlighter palette — soft pastels that read under dark serif text.
    COLORS: ['#f7b6d2', '#ffd9a0', '#a0e6b4', '#a8dcf0', '#d6b8f0', '#f5e79e'],
    CENSOR: '#12100f',

    // Background presets (drawn as canvas gradients).
    BACKGROUNDS: {
        blush: { label: 'Blush', stops: ['#f6d5d0', '#c9a7c8'] },
        dusk: { label: 'Dusk', stops: ['#2b2140', '#4b3a63'] },
        ink: { label: 'Ink', stops: ['#14161c', '#242a38'] },
        sage: { label: 'Sage', stops: ['#d7e6cf', '#a7c4a0'] },
        peach: { label: 'Peach', stops: ['#ffe3c2', '#f3b7a6'] },
        white: { label: 'Plain', stops: ['#ececf0', '#dcdce4'] },
    },

    // Font families. Lora ships in frontend/fonts (OFL); the sans stays system.
    FAMILIES: {
        lora: { label: 'Lora (book look)', css: "Lora, Georgia, 'Times New Roman', serif" },
        sans: { label: 'Sans', css: "'Helvetica Neue', Arial, sans-serif" },
    },

    STYLE_FLAGS: ['b', 'i', 'u', 's', 'x'],   // x = scratched out (censor), a style so a highlight stays under it

    // Page metrics (matched to the reference card, 4.16.0): leading 1.42, a
    // first-line indent of 1.2 em per paragraph, highlight blocks that fill the
    // whole line box so a run that wraps forms one solid shape.
    LEADING: 1.42,
    INDENT_EM: 1.2,

    _state: null,
    _fontsRequested: false,
    _promoId: null,        // the saved row this card is, or null for an unsaved one (4.16.0)
    _dirty: false,
    _bgDirty: false,       // a new photo was picked since the last save
    _bgCleared: false,     // the saved photo was removed since the last save

    // ── state ────────────────────────────────────────────────────────────────

    defaultState() {
        const sample = '"What?"\n"I don\'t have any idea what you mean."\n'
            + 'She smirks, and something in the room shifts. Slowly, she leans in, '
            + 'her voice dropping to almost nothing. "You know exactly what I mean," '
            + 'she says. "Right where it belongs."';
        return {
            version: 2,
            text: sample,
            highlights: [],
            styles: [],
            size: { preset: 'portrait', w: 1080, h: 1350 },
            bg: 'blush',
            bgImage: null,      // Image() for drawing
            bgFile: null,       // the original File (kept for saving, release 2)
            font: { family: 'lora', px: 60 },
            footer: '',
            story: null,
            title: '',
            pages: null,        // release 3: [{text, highlights, styles}]; the top-level trio is the page being edited
            page: 0,
            color: this.COLORS[0],
        };
    },

    // ── pages (release 3) ────────────────────────────────────────────────────

    _ensurePages(s) {
        if (!Array.isArray(s.pages) || !s.pages.length) {
            s.pages = [{ text: s.text, highlights: s.highlights.map(h => ({ ...h })), styles: s.styles.map(r => ({ ...r })) }];
            s.page = 0;
        }
        return s.pages;
    },

    /* Copy the page being edited (the top-level text / ranges) back into pages[]. */
    _commitPage() {
        const s = this._state;
        this._ensurePages(s);
        s.page = Math.min(Math.max(0, s.page | 0), s.pages.length - 1);
        s.pages[s.page] = { text: s.text, highlights: s.highlights.map(h => ({ ...h })), styles: s.styles.map(r => ({ ...r })) };
    },

    _gotoPage(i) {
        const s = this._state;
        this._commitPage();
        i = Math.min(Math.max(0, i | 0), s.pages.length - 1);
        s.page = i;
        const p = s.pages[i];
        s.text = p.text; s.highlights = p.highlights.map(h => ({ ...h })); s.styles = p.styles.map(r => ({ ...r }));
        const wasDirty = this._dirty;
        this.render();
        this._dirty = wasDirty;
        this._paintSaveState();
    },

    _addPage(duplicate = false) {
        const s = this._state;
        this._commitPage();
        const cur = s.pages[s.page];
        s.pages.splice(s.page + 1, 0, duplicate
            ? { text: cur.text, highlights: cur.highlights.map(h => ({ ...h })), styles: cur.styles.map(r => ({ ...r })) }
            : { text: '', highlights: [], styles: [] });
        this._gotoPage(s.page + 1);
        this._dirty = true;
        this._paintSaveState();
    },

    _deletePage() {
        const s = this._state;
        this._commitPage();
        if (s.pages.length <= 1) return;
        s.pages.splice(s.page, 1);
        const next = Math.min(s.page, s.pages.length - 1);
        s.page = -1;                                  // force _gotoPage to reload the trio
        const p = s.pages[next];
        s.text = p.text; s.highlights = p.highlights.map(h => ({ ...h })); s.styles = p.styles.map(r => ({ ...r }));
        s.page = next;
        this.render();
        this._dirty = true;
        this._paintSaveState();
    },

    /* The full state for page i (shared settings + that page's text and ranges). */
    pageState(i) {
        const s = this._state;
        this._commitPage();
        const p = s.pages[Math.min(Math.max(0, i | 0), s.pages.length - 1)];
        return { ...s, text: p.text, highlights: p.highlights, styles: p.styles };
    },

    /* Render page i to a PNG blob on an offscreen canvas. */
    renderPageBlob(i) {
        const c = document.createElement('canvas');
        this.paintCard(c, this.pageState(i));
        return new Promise(res => c.toBlob(res, 'image/png'));
    },

    // ── zip (stored, no compression; ~60 lines so no library ships) ─────────

    _crcTable: null,
    crc32(bytes) {
        if (!this._crcTable) {
            const t = new Uint32Array(256);
            for (let n = 0; n < 256; n++) {
                let c = n;
                for (let k = 0; k < 8; k++) c = (c & 1) ? (0xEDB88320 ^ (c >>> 1)) : (c >>> 1);
                t[n] = c >>> 0;
            }
            this._crcTable = t;
        }
        let crc = 0xFFFFFFFF;
        for (let i = 0; i < bytes.length; i++) crc = this._crcTable[(crc ^ bytes[i]) & 0xFF] ^ (crc >>> 8);
        return (crc ^ 0xFFFFFFFF) >>> 0;
    },

    /* entries: [{name, data: Uint8Array}] → Uint8Array of a ZIP with stored entries.
     * Deterministic (fixed DOS timestamp) so two exports of the same pages are identical. */
    zipStore(entries) {
        const enc = new TextEncoder();
        const parts = [];
        const central = [];
        let offset = 0;
        const dosTime = (0 << 11) | (0 << 5) | 0;                // 00:00:00
        const dosDate = ((2026 - 1980) << 9) | (1 << 5) | 1;     // 2026-01-01
        const u16 = v => [v & 0xFF, (v >>> 8) & 0xFF];
        const u32 = v => [v & 0xFF, (v >>> 8) & 0xFF, (v >>> 16) & 0xFF, (v >>> 24) & 0xFF];
        entries.forEach(e => {
            const name = enc.encode(e.name);
            const data = e.data instanceof Uint8Array ? e.data : new Uint8Array(e.data);
            const crc = this.crc32(data);
            const local = new Uint8Array([
                ...u32(0x04034b50), ...u16(20), ...u16(0x0800), ...u16(0), ...u16(dosTime), ...u16(dosDate),
                ...u32(crc), ...u32(data.length), ...u32(data.length), ...u16(name.length), ...u16(0)]);
            parts.push(local, name, data);
            central.push(new Uint8Array([
                ...u32(0x02014b50), ...u16(20), ...u16(20), ...u16(0x0800), ...u16(0), ...u16(dosTime), ...u16(dosDate),
                ...u32(crc), ...u32(data.length), ...u32(data.length), ...u16(name.length), ...u16(0), ...u16(0),
                ...u16(0), ...u16(0), ...u32(0), ...u32(offset)]), name);
            offset += local.length + name.length + data.length;
        });
        const cdStart = offset;
        let cdLen = 0;
        central.forEach(c => { cdLen += c.length; });
        const eocd = new Uint8Array([
            ...u32(0x06054b50), ...u16(0), ...u16(0), ...u16(entries.length), ...u16(entries.length),
            ...u32(cdLen), ...u32(cdStart), ...u16(0)]);
        const total = offset + cdLen + eocd.length;
        const out = new Uint8Array(total);
        let pos = 0;
        [...parts, ...central, eocd].forEach(chunk => { out.set(chunk, pos); pos += chunk.length; });
        return out;
    },

    _exportBase() {
        const s = this._state;
        const slug = String(s.title || '').trim().toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-+|-+$/g, '');
        return `pawpoller-promo-${slug || (this._promoId ? this._promoId : Date.now())}`;
    },

    async _downloadAll() {
        const s = this._state;
        this._ensurePages(s);
        if (s.pages.length === 1) return this._download();
        const entries = [];
        for (let i = 0; i < s.pages.length; i++) {
            const blob = await this.renderPageBlob(i);
            entries.push({ name: `${this._exportBase()}-${i + 1}.png`, data: new Uint8Array(await blob.arrayBuffer()) });
        }
        const zip = new Blob([this.zipStore(entries)], { type: 'application/zip' });
        const url = URL.createObjectURL(zip);
        const a = document.createElement('a');
        a.href = url; a.download = `${this._exportBase()}.zip`;
        document.body.appendChild(a); a.click(); a.remove();
        setTimeout(() => URL.revokeObjectURL(url), 1000);
    },

    // ── saved cards (release 2) ──────────────────────────────────────────────

    /* The editable spec — everything but the live Image/File handles. */
    toSpec() {
        const s = this._state;
        this._commitPage();
        const pages = s.pages.map(p => ({ text: p.text, highlights: p.highlights.map(h => ({ ...h })), styles: p.styles.map(r => ({ ...r })) }));
        return {
            version: 2,
            text: pages[0].text,                       // page 1 doubles as the single-card shape
            highlights: pages[0].highlights,
            styles: pages[0].styles,
            pages,
            size: { ...s.size },
            bg: s.bg,
            bgImage: s.bgImage ? { asset: 'background' } : null,
            font: { ...s.font },
            footer: s.footer,
            story: s.story || null,
            title: s.title || '',
        };
    },

    fromSpec(spec, row) {
        const d = this.defaultState();
        const size = spec.size || {};
        const c = this.clampSize(size.w, size.h);
        const norm = (pg) => {
            const hls = Array.isArray(pg.highlights) ? pg.highlights : [];
            const sts = Array.isArray(pg.styles) ? pg.styles.slice() : [];
            hls.filter(h => h && h.censor).forEach(h => sts.push({ start: h.start, end: h.end, x: true }));   // pre-4.16.0 shape
            return { text: typeof pg.text === 'string' ? pg.text : '', highlights: hls.filter(h => h && !h.censor), styles: sts };
        };
        const pages = (Array.isArray(spec.pages) && spec.pages.length ? spec.pages : [spec]).map(norm);
        if (!pages[0].text && typeof spec.text !== 'string') pages[0].text = d.text;
        return {
            ...d,
            text: pages[0].text,
            highlights: pages[0].highlights,
            styles: pages[0].styles,
            pages,
            page: 0,
            size: { preset: this.SIZES[size.preset] ? size.preset : 'custom', w: c.w, h: c.h },
            bg: this.BACKGROUNDS[spec.bg] ? spec.bg : d.bg,
            font: { family: this.FAMILIES[(spec.font || {}).family] ? spec.font.family : 'lora',
                    px: Math.min(160, Math.max(28, parseInt((spec.font || {}).px, 10) || 60)) },
            footer: typeof spec.footer === 'string' ? spec.footer : '',
            story: (row && row.story_name) || spec.story || null,
            title: (row && row.title) || spec.title || '',
        };
    },

    /* Load a saved card into the state. Resolves false when it no longer exists. */
    async open(id) {
        let row;
        try { row = await API.getPromo(id); } catch (e) { return false; }
        if (!row || !row.spec) return false;
        this._state = this.fromSpec(row.spec, row);
        this._promoId = row.promo_id;
        this._bgDirty = false; this._bgCleared = false;
        if (row.has_background) {
            const url = `/api/promos/${row.promo_id}/background?t=${Date.now()}`;
            const img = new Image();
            img.onload = () => {
                this._state.bgImage = img;
                const b = document.getElementById('promo-bgclear');
                if (b) b.disabled = false;
                this.draw();
                this._dirty = false;
                this._paintSaveState();
            };
            img.src = url;
            // Keep the original bytes too, so "Save as new" carries the photo across.
            fetch(url).then(r => r.ok ? r.blob() : null).then(b => {
                if (b) this._state.bgFile = new File([b], 'background', { type: b.type || 'image/jpeg' });
            }).catch(() => {});
        }
        return true;
    },

    _paintSaveState() {
        const btn = document.getElementById('promo-save');
        if (btn) btn.textContent = this._dirty ? '💾 Save •' : '💾 Save';
        const again = document.getElementById('promo-saveas');
        if (again) again.hidden = !this._promoId;
        const note = document.getElementById('promo-story-note');
        if (note) {
            const s = this._state;
            note.textContent = s.story
                ? `Attached to story: ${s.story}` + (this._promoId ? ` · saved as #${this._promoId}` : '')
                : 'Not attached to a story — pull an excerpt from one to attach it.' + (this._promoId ? ` Saved as #${this._promoId}.` : '');
        }
    },

    _save(asNew = false) {
        const canvas = document.getElementById('promo-canvas');
        if (!canvas) return;
        const msg = document.getElementById('promo-save-msg');
        const btn = document.getElementById('promo-save');
        if (btn) btn.disabled = true;
        if (msg) msg.textContent = 'Saving…';
        (async () => {
            const blob = await this.renderPageBlob(0);          // page 1 is the card the server keeps
            try {
                if (!blob) throw new Error('Could not render the card');
                const s = this._state;
                const creating = asNew || !this._promoId;
                const fd = new FormData();
                fd.append('spec', JSON.stringify(this.toSpec()));
                fd.append('png', new File([blob], 'promo.png', { type: 'image/png' }));
                fd.append('title', s.title || '');
                fd.append('story_name', s.story || '');
                if (s.bgFile && (creating || this._bgDirty)) fd.append('background', s.bgFile);
                if (!creating && this._bgCleared && !s.bgFile) fd.append('clear_background', '1');
                const row = creating ? await API.createPromo(fd) : await API.updatePromo(this._promoId, fd);
                this._promoId = row.promo_id;
                this._bgDirty = false; this._bgCleared = false; this._dirty = false;
                // Change the address without a re-render (hashchange does not fire on replaceState).
                history.replaceState(null, '', `#/promo/${row.promo_id}`);
                if (msg) msg.textContent = `Saved${s.story ? ' to ' + s.story : ''} · ${new Date().toLocaleTimeString()}`;
                this._paintSaveState();
            } catch (e) {
                if (msg) msg.innerHTML = `<span style="color:var(--danger)">${Utils.escapeHtml(e.message || String(e))}</span>`;
            } finally {
                if (btn) btn.disabled = false;
            }
        })();
    },

    // ── render ───────────────────────────────────────────────────────────────

    async render(id = null, query = '') {
        const app = document.getElementById('app');
        if (id === 'new') {
            this._state = this.defaultState();
            this._promoId = null; this._bgDirty = false; this._bgCleared = false;
        } else if (id && String(id) !== String(this._promoId)) {
            app.innerHTML = '<div class="loading-spinner">Opening the promo…</div>';
            const ok = await this.open(id);
            if (!ok) {
                app.innerHTML = `<div class="page-header"><h1>✨ Promo Maker</h1></div>
                    <div class="card error">That promo no longer exists. <a href="#/promo/new">Make a new one</a></div>`;
                return;
            }
        }
        this._state = this._state || this.defaultState();
        const s = this._state;

        const swatches = this.COLORS.map(c =>
            `<button type="button" class="promo-swatch${c === s.color ? ' is-active' : ''}" `
            + `data-color="${c}" style="background:${c}" title="Highlight in this colour"></button>`).join('');
        const sizeOpts = Object.entries(this.SIZES).map(([k, v]) =>
            `<option value="${k}"${k === s.size.preset ? ' selected' : ''}>${v.label}</option>`).join('');
        const famOpts = Object.entries(this.FAMILIES).map(([k, v]) =>
            `<option value="${k}"${k === s.font.family ? ' selected' : ''}>${v.label}</option>`).join('');
        const bgSwatches = Object.entries(this.BACKGROUNDS).map(([k, v]) =>
            `<button type="button" class="promo-bg${k === s.bg ? ' is-active' : ''}" data-bg="${k}" `
            + `style="background:linear-gradient(135deg,${v.stops[0]},${v.stops[1]})" title="${v.label}"></button>`).join('');
        const custom = s.size.preset === 'custom';

        app.innerHTML = `
            <div class="page-header">
                <h1>✨ Promo Maker</h1>
                <p class="muted">Turn a spicy excerpt into a shareable image. Paste your text, select a phrase and
                tap a colour to highlight it or a style to format it, pick a background and size, then download.
                Great for Instagram, TikTok covers, Bluesky and Threads.</p>
            </div>
            <div class="promo-layout">
                <div class="promo-controls">
                    <div class="card">
                        <div class="promo-src-row">
                            <button type="button" class="btn btn-sm" id="promo-from-story">📖 Pull from a story</button>
                            <span class="muted">or paste your own below</span>
                        </div>
                        <div class="promo-pages" id="promo-pages" role="toolbar" aria-label="Pages">
                            <span class="muted" style="font-size:12px">Pages</span>
                            ${this._ensurePages(s).map((p, i) => `<button type="button" class="btn btn-sm promo-page${i === s.page ? ' is-active' : ''}" data-page="${i}" title="Page ${i + 1}">${i + 1}</button>`).join('')}
                            <button type="button" class="btn btn-sm" id="promo-page-add" title="Add an empty page after this one">+ Page</button>
                            <button type="button" class="btn btn-sm" id="promo-page-dup" title="Duplicate this page">Duplicate</button>
                            <button type="button" class="btn btn-sm" id="promo-page-del" ${s.pages.length > 1 ? '' : 'disabled'} title="Delete this page">Delete page</button>
                        </div>
                        <label class="field">Excerpt${s.pages.length > 1 ? ` <span class="muted">(page ${s.page + 1} of ${s.pages.length})</span>` : ''}
                            <textarea id="promo-text" rows="8" spellcheck="false"
                                placeholder="Paste a passage from your story…">${Utils.escapeHtml(s.text)}</textarea>
                        </label>
                        <div class="promo-hint muted">Select some words above, then tap a colour to highlight them or a
                        style to format them (Ctrl+B / I / U, Ctrl+Shift+X for strikethrough).</div>
                        <div class="promo-swatches">${swatches}
                            <button type="button" class="promo-swatch promo-swatch--censor" id="promo-censor"
                                title="Black out the selected words (censor bar)"></button>
                            <button type="button" class="btn btn-sm" id="promo-clearhl" title="Remove every highlight and censor bar">Clear</button>
                        </div>
                        <div class="promo-styles" role="toolbar" aria-label="Text style">
                            <button type="button" class="btn btn-sm promo-style" data-flag="b" title="Bold (Ctrl+B)"><b>B</b></button>
                            <button type="button" class="btn btn-sm promo-style" data-flag="i" title="Italic (Ctrl+I)"><i>I</i></button>
                            <button type="button" class="btn btn-sm promo-style" data-flag="u" title="Underline (Ctrl+U)"><u>U</u></button>
                            <button type="button" class="btn btn-sm promo-style" data-flag="s" title="Strikethrough (Ctrl+Shift+X)"><s>S</s></button>
                            <button type="button" class="btn btn-sm" id="promo-clearst" title="Remove every bold / italic / underline / strike">Clear formatting</button>
                        </div>
                    </div>
                    <div class="card">
                        <div class="field-row">
                            <label class="field">Size
                                <select id="promo-size">${sizeOpts}</select>
                            </label>
                            <label class="field">Text size
                                <input type="range" id="promo-font" min="28" max="160" step="2" value="${s.font.px}">
                            </label>
                        </div>
                        <div class="field-row promo-size-custom" id="promo-size-custom"${custom ? '' : ' hidden'}>
                            <label class="field">Width (px)
                                <input type="number" id="promo-w" min="${this.MIN_SIDE}" max="${this.MAX_SIDE}" step="1" value="${s.size.w}">
                            </label>
                            <label class="field">Height (px)
                                <input type="number" id="promo-h" min="${this.MIN_SIDE}" max="${this.MAX_SIDE}" step="1" value="${s.size.h}">
                            </label>
                        </div>
                        <label class="field">Font
                            <select id="promo-family">${famOpts}</select>
                        </label>
                        <div class="field" style="margin-top:.6rem">Background
                            <div class="promo-bgs">${bgSwatches}</div>
                        </div>
                        <div class="field-row" style="margin-top:.6rem">
                            <label class="btn btn-sm" style="cursor:pointer">📷 Photo background
                                <input type="file" id="promo-bgimg" accept="image/*" hidden>
                            </label>
                            <button type="button" class="btn btn-sm" id="promo-bgclear" ${s.bgImage ? '' : 'disabled'}>Remove photo</button>
                        </div>
                        <label class="field" style="margin-top:.6rem">Footer / handle <span class="muted">(optional)</span>
                            <input type="text" id="promo-footer" value="${Utils.escapeHtml(s.footer)}" placeholder="@yourhandle · Read now">
                        </label>
                        <div class="promo-handle-row">
                            <select id="promo-handle" class="promo-handle" title="Insert one of your account handles">
                                <option value="">Use a handle…</option>
                            </select>
                        </div>
                    </div>
                    <div class="card promo-save">
                        <label class="field">Title <span class="muted">(for the story's Promos list)</span>
                            <input type="text" id="promo-title" value="${Utils.escapeHtml(s.title || '')}" placeholder="e.g. Chapter 3 teaser">
                        </label>
                        <div class="promo-save-row">
                            <button type="button" class="btn btn-primary btn-sm" id="promo-save">💾 Save</button>
                            <button type="button" class="btn btn-sm" id="promo-saveas" ${this._promoId ? '' : 'hidden'} title="Keep this one and save a copy as a new card">Save as new</button>
                            <span id="promo-save-msg" class="muted"></span>
                        </div>
                        <div class="muted promo-save-note" id="promo-story-note"></div>
                    </div>
                    <div class="promo-actions">
                        <button class="btn btn-primary" id="promo-download">⬇ Download PNG</button>
                        <button class="btn" id="promo-download-all" ${s.pages.length > 1 ? '' : 'hidden'} title="Every page as a numbered PNG inside one zip">⬇ All pages (zip)</button>
                        <button class="btn" id="promo-share" title="Open the post composer with every page attached">💬 Send to Posts</button>
                        <span id="promo-warn" class="promo-warn"></span>
                    </div>
                </div>
                <div class="promo-preview">
                    <canvas id="promo-canvas"></canvas>
                </div>
            </div>`;

        this._wire();
        this._loadHandles();
        this.draw();
        this._ensureFonts();
        this._dirty = false;
        this._paintSaveState();
        if (id === 'new') {
            const story = new URLSearchParams(query || '').get('story');
            if (story) this._openStoryPicker(story);
        }
    },

    _wire() {
        const s = this._state;
        const $ = id => document.getElementById(id);
        const ta = $('promo-text');

        ta.addEventListener('input', e => {
            // Text changed — character offsets shift, so old ranges no longer map
            // cleanly. Keep those that still fit within the new length.
            s.text = e.target.value;
            s.highlights = s.highlights.filter(h => h.end <= s.text.length);
            s.styles = s.styles.filter(h => h.end <= s.text.length);
            this.draw();
        });
        ta.addEventListener('keydown', e => {
            if (!(e.ctrlKey || e.metaKey)) return;
            const k = e.key.toLowerCase();
            let flag = null;
            if (k === 'b') flag = 'b';
            else if (k === 'i') flag = 'i';
            else if (k === 'u') flag = 'u';
            else if (k === 'x' && e.shiftKey) flag = 's';
            if (!flag) return;
            e.preventDefault();
            this._toggleStyle(flag, ta.selectionStart, ta.selectionEnd);
        });

        // Swatches + style buttons must not steal focus — otherwise the click
        // blurs the textarea and some browsers collapse its selection before we
        // can read it. mousedown-preventDefault keeps the selection alive.
        document.querySelectorAll('.promo-swatch').forEach(b => {
            b.addEventListener('mousedown', e => e.preventDefault());
            b.addEventListener('click', () => {
                s.color = b.dataset.color;
                document.querySelectorAll('.promo-swatch').forEach(x => x.classList.toggle('is-active', x === b));
                this._applyHighlight();
            });
        });
        const censor = $('promo-censor');
        censor.addEventListener('mousedown', e => e.preventDefault());
        censor.addEventListener('click', () => this._applyHighlight(true));
        $('promo-clearhl').addEventListener('click', () => { s.highlights = []; this.draw(); });

        document.querySelectorAll('.promo-style').forEach(b => {
            b.addEventListener('mousedown', e => e.preventDefault());
            b.addEventListener('click', () => this._toggleStyle(b.dataset.flag, ta.selectionStart, ta.selectionEnd));
        });
        $('promo-clearst').addEventListener('click', () => { s.styles = []; this.draw(); });

        $('promo-size').addEventListener('change', e => {
            const preset = e.target.value;
            const box = $('promo-size-custom');
            if (preset === 'custom') {
                s.size = { preset, w: s.size.w, h: s.size.h };
                box.hidden = false;
            } else {
                const p = this.SIZES[preset] || this.SIZES.portrait;
                s.size = { preset, w: p.w, h: p.h };
                box.hidden = true;
                $('promo-w').value = p.w; $('promo-h').value = p.h;
            }
            this.draw();
        });
        const onCustom = () => {
            const c = this.clampSize(parseInt($('promo-w').value, 10), parseInt($('promo-h').value, 10));
            s.size = { preset: 'custom', w: c.w, h: c.h };
            this.draw();
        };
        $('promo-w').addEventListener('change', onCustom);
        $('promo-h').addEventListener('change', onCustom);

        $('promo-font').addEventListener('input', e => { s.font.px = parseInt(e.target.value, 10); this.draw(); });
        $('promo-family').addEventListener('change', e => {
            s.font.family = e.target.value;
            this.draw();
            this._ensureFonts();
        });
        $('promo-footer').addEventListener('input', e => { s.footer = e.target.value; this.draw(); });
        $('promo-handle').addEventListener('change', e => {
            const h = e.target.value;
            if (!h) return;
            s.footer = this.footerWithHandle(s.footer, h);
            $('promo-footer').value = s.footer;
            e.target.value = '';
            this.draw();
        });

        document.querySelectorAll('.promo-bg').forEach(b =>
            b.addEventListener('click', () => {
                s.bg = b.dataset.bg;
                if (s.bgImage) this._bgCleared = true;
                s.bgImage = null; s.bgFile = null; this._bgDirty = false;
                $('promo-bgclear').disabled = true;
                document.querySelectorAll('.promo-bg').forEach(x => x.classList.toggle('is-active', x === b));
                this.draw();
            }));

        $('promo-bgimg').addEventListener('change', e => {
            const file = e.target.files && e.target.files[0];
            if (!file) return;
            const img = new Image();
            img.onload = () => {
                s.bgImage = img; s.bgFile = file; this._bgDirty = true; this._bgCleared = false;
                $('promo-bgclear').disabled = false; this.draw();
            };
            img.src = URL.createObjectURL(file);
        });
        $('promo-bgclear').addEventListener('click', () => {
            if (s.bgImage) this._bgCleared = true;
            s.bgImage = null; s.bgFile = null; this._bgDirty = false; $('promo-bgclear').disabled = true; this.draw();
        });

        $('promo-download').addEventListener('click', () => this._download());
        $('promo-download-all').addEventListener('click', () => this._downloadAll().catch(err => { if (window.toast && toast.error) toast.error(err.message || String(err)); }));
        document.querySelectorAll('.promo-page').forEach(b => b.addEventListener('click', () => this._gotoPage(parseInt(b.dataset.page, 10))));
        $('promo-page-add').addEventListener('click', () => this._addPage(false));
        $('promo-page-dup').addEventListener('click', () => this._addPage(true));
        $('promo-page-del').addEventListener('click', () => this._deletePage());
        $('promo-share').addEventListener('click', () => this._shareToPosts());
        $('promo-from-story').addEventListener('click', () => this._openStoryPicker());
        $('promo-title').addEventListener('input', e => { s.title = e.target.value; this._dirty = true; this._paintSaveState(); });
        $('promo-save').addEventListener('click', () => this._save(false));
        $('promo-saveas').addEventListener('click', () => this._save(true));
    },

    /* The footer's "Use a handle…" menu: every enabled account's handle, once. */
    async _loadHandles() {
        const sel = document.getElementById('promo-handle');
        if (!sel || typeof API === 'undefined' || !API.getAccounts) return;   // API is a lexical global, not window.API
        try {
            const d = await API.getAccounts();
            const names = (d && d.platform_names) || {};
            const seen = new Set();
            (d && d.accounts || []).forEach(a => {
                const h = String(a.handle || '').trim().replace(/^@/, '');
                if (!h || a.enabled === 0 || a.enabled === false || seen.has(h.toLowerCase())) return;
                seen.add(h.toLowerCase());
                const opt = document.createElement('option');
                opt.value = h;
                opt.textContent = `@${h} · ${names[a.platform] || a.platform}`;
                sel.appendChild(opt);
            });
            sel.hidden = seen.size === 0;
        } catch (e) {
            sel.hidden = true;
        }
    },

    /* Insert @handle into a footer: replaces an existing @token, keeps any
     * " · suffix" the person typed. Pure. */
    footerWithHandle(footer, handle) {
        const h = '@' + String(handle || '').replace(/^@/, '');
        const cur = String(footer || '');
        if (/@[\w.]+/.test(cur)) return cur.replace(/@[\w.]+/, h);
        return cur.trim() ? `${h} · ${cur.trim()}` : h;
    },

    /* Load the bundled Lora faces (regular / bold / italic / bold italic) once,
     * then redraw — the first paint may otherwise use the fallback serif. */
    _ensureFonts() {
        if (this._fontsRequested || typeof document === 'undefined' || !document.fonts || !document.fonts.load) return;
        if (this._state.font.family !== 'lora') return;
        this._fontsRequested = true;
        const faces = ['400 60px Lora', '700 60px Lora', 'italic 400 60px Lora', 'italic 700 60px Lora'];
        Promise.all(faces.map(f => document.fonts.load(f).catch(() => null)))
            .then(() => {
                // A font-ready repaint is not a change the person made.
                const wasDirty = this._dirty;
                this.draw();
                this._dirty = wasDirty;
                this._paintSaveState();
            })
            .catch(() => { /* fallback serif stays */ });
    },

    // ── hand-off / download ──────────────────────────────────────────────────

    /* Hand the rendered card straight to the post composer (Create → New post)
     * with the image already attached, so a promo can go out without a
     * download/re-upload round trip. */
    _shareToPosts() {
        const canvas = document.getElementById('promo-canvas');
        if (!canvas) return;
        if (!window.Posts) {
            if (window.toast && toast.error) toast.error('Posts module unavailable');
            return;
        }
        (async () => {
            const s = this._state;
            this._ensurePages(s);
            const files = [];
            for (let i = 0; i < s.pages.length; i++) {
                const blob = await this.renderPageBlob(i);
                if (blob) files.push(new File([blob], `${this._exportBase()}-${i + 1}.png`, { type: 'image/png' }));
            }
            if (!files.length) return;
            // Picked up by Posts.renderCompose() once the composer has rendered.
            window.Posts._handoffFiles = files;
            window.location.hash = '#/posts/new';
        })();
    },

    _download() {
        const canvas = document.getElementById('promo-canvas');
        if (!canvas) return;
        canvas.toBlob(blob => {
            if (!blob) return;
            const url = URL.createObjectURL(blob);
            const a = document.createElement('a');
            a.href = url;
            a.download = `pawpoller-promo-${Date.now()}.png`;
            document.body.appendChild(a);
            a.click();
            a.remove();
            setTimeout(() => URL.revokeObjectURL(url), 1000);
        }, 'image/png');
    },

    // ── "Pull from a story" picker ───────────────────────────────────────────
    /* Loads a story's MASTER.md via the editor API, shows it read-only, and
     * lifts whatever the user selects into the excerpt box. Markdown/metadata is
     * stripped so the canvas renders clean prose. */
    async _openStoryPicker(presetName = null) {
        let ov = document.getElementById('promo-story-ov');
        if (!ov) {
            ov = document.createElement('div');
            ov.id = 'promo-story-ov';
            ov.className = 'promo-ov';
            ov.innerHTML = `
                <div class="promo-ovbox">
                    <h3>Pull an excerpt from a story</h3>
                    <p class="muted">Pick a story, select the passage you want, then hit <strong>Use selection</strong>.</p>
                    <div class="promo-story-pickrow">
                        <button class="btn btn-sm" id="promo-story-pick" type="button">🔍 Choose a story…</button>
                        <span id="promo-story-chosen" class="muted">No story chosen yet</span>
                    </div>
                    <textarea id="promo-story-src" class="promo-src" rows="14" readonly
                        placeholder="Pick a story to load its text…"></textarea>
                    <div class="promo-ov-actions">
                        <button class="btn btn-primary btn-sm" id="promo-use-sel">Use selection</button>
                        <button class="btn btn-sm" id="promo-story-close">Cancel</button>
                        <span id="promo-story-msg" class="muted"></span>
                    </div>
                </div>`;
            document.body.appendChild(ov);
        }
        ov.classList.add('open');
        const src = document.getElementById('promo-story-src');
        const msg = document.getElementById('promo-story-msg');
        const chosen = document.getElementById('promo-story-chosen');
        msg.textContent = '';
        let pickedName = null;
        const close = () => ov.classList.remove('open');
        document.getElementById('promo-story-close').onclick = close;
        ov.onclick = e => { if (e.target === ov) close(); };

        // Story chosen via the visual WorkPicker (2.162.0). Single-select,
        // stories only; on confirm we load that story's text into the
        // read-only pane for the user to lift a passage from.
        document.getElementById('promo-story-pick').onclick = () => {
            if (!window.WorkPicker) { msg.textContent = 'Picker unavailable.'; return; }
            WorkPicker.open({
                title: 'Pick a story',
                confirmLabel: 'Load story',
                multi: false,
                filters: ['story'],
                onConfirm: async (items) => {
                    const it = items[0];
                    if (!it) return;
                    const name = it.member_ref.split(':').slice(1).join(':') || it.member_ref;
                    pickedName = name;
                    if (chosen) { chosen.textContent = it.title; chosen.classList.remove('muted'); }
                    src.value = 'Loading…';
                    try {
                        const d = await API.getEditorStoryContent(name);
                        src.value = this._stripMd(d && d.content);
                    } catch (e) {
                        src.value = 'Could not load that story.';
                    }
                },
            });
        };

        // Opened from a story board (#/promo/new?story=…): load that story straight away.
        if (presetName) {
            pickedName = presetName;
            if (chosen) { chosen.textContent = presetName; chosen.classList.remove('muted'); }
            src.value = 'Loading…';
            try {
                const d = await API.getEditorStoryContent(presetName);
                src.value = this._stripMd(d && d.content);
            } catch (e) {
                src.value = 'Could not load that story.';
            }
        }

        document.getElementById('promo-use-sel').onclick = () => {
            const picked = src.value.slice(src.selectionStart, src.selectionEnd).trim();
            if (!picked) { msg.textContent = 'Select some text in the story first.'; return; }
            const s = this._state;
            s.text = picked;
            s.highlights = [];   // character offsets no longer map to the new text
            s.styles = [];
            s.story = pickedName;
            document.getElementById('promo-text').value = picked;
            close();
            this.draw();
        };
    },

    /* MASTER.md → plain prose: drop metadata comments, headings, emphasis and
     * scene-break rules so the card renders the words, not the markup. */
    _stripMd(md) {
        return String(md || '')
            .replace(/<!--[\s\S]*?-->/g, '')              // <!-- @title --> metadata
            .replace(/^#{1,6}\s+/gm, '')                  // headings
            .replace(/\*\*(.*?)\*\*/g, '$1')              // bold
            .replace(/(^|[^*])\*([^*\n]+)\*/g, '$1$2')    // italic narration
            .replace(/^\s*---\s*$/gm, '')                 // scene breaks
            .replace(/\n{3,}/g, '\n\n')
            .trim();
    },

    // ── ranges: highlights and styles (pure) ─────────────────────────────────

    /* Turn the current textarea selection into a highlight range in the active
     * colour (or a censor bar). A new range drops any it fully covers and trims
     * partial overlaps so colours never stack. */
    _applyHighlight(censor = false) {
        const ta = document.getElementById('promo-text');
        const start = ta.selectionStart, end = ta.selectionEnd;
        if (start === end) return;              // nothing selected — just switched colour
        const s = this._state;
        if (censor) {                           // scratch out: a style flag, toggled like bold
            this._toggleStyle('x', start, end);
            return;
        }
        s.highlights = this.addHighlight(s.highlights, start, end, { color: s.color });
        this.draw();
    },

    addHighlight(highlights, start, end, rec) {
        const out = [];
        highlights.forEach(h => {
            if (h.end <= start || h.start >= end) { out.push(h); return; }
            // Partial overlaps: keep the parts outside the new range.
            if (h.start < start) out.push({ ...h, end: start });
            if (h.end > end) out.push({ ...h, start: end });
        });
        out.push({ start, end, color: rec.color });
        return out.sort((a, b) => a.start - b.start);
    },

    /* Per-character style flags over [0, len): the OR of every covering range. */
    styleMap(styles, len) {
        const map = new Array(len);
        for (let i = 0; i < len; i++) map[i] = { b: false, i: false, u: false, s: false, x: false };
        styles.forEach(r => {
            const a = Math.max(0, r.start), z = Math.min(len, r.end);
            for (let i = a; i < z; i++) {
                if (r.b) map[i].b = true;
                if (r.i) map[i].i = true;
                if (r.u) map[i].u = true;
                if (r.s) map[i].s = true;
                if (r.x) map[i].x = true;
            }
        });
        return map;
    },

    /* Minimal ranges from a per-character map: consecutive characters with the
     * same flags become one range; all-false stretches are dropped. */
    coalesceStyles(map) {
        const out = [];
        let cur = null;
        const same = (a, b) => a.b === b.b && a.i === b.i && a.u === b.u && a.s === b.s && a.x === b.x;
        const any = f => f.b || f.i || f.u || f.s || f.x;
        for (let i = 0; i < map.length; i++) {
            const f = map[i];
            if (cur && same(cur, f)) { cur.end = i + 1; continue; }
            if (cur && any(cur)) out.push(cur);
            cur = { start: i, end: i + 1, b: f.b, i: f.i, u: f.u, s: f.s, x: f.x };
        }
        if (cur && any(cur)) out.push(cur);
        return out;
    },

    /* Toggle one flag over a selection: if EVERY selected character already has
     * it, remove it; otherwise add it. Returns the new styles list. Pure. */
    toggleStyle(styles, len, flag, start, end) {
        start = Math.max(0, Math.min(start, len));
        end = Math.max(0, Math.min(end, len));
        if (start >= end) return styles;
        const map = this.styleMap(styles, len);
        let all = true;
        for (let i = start; i < end; i++) if (!map[i][flag]) { all = false; break; }
        for (let i = start; i < end; i++) map[i][flag] = !all;
        return this.coalesceStyles(map);
    },

    _toggleStyle(flag, start, end) {
        const s = this._state;
        if (start === end) return;
        s.styles = this.toggleStyle(s.styles, s.text.length, flag, start, end);
        this.draw();
    },

    clampSize(w, h) {
        const c = v => Math.min(this.MAX_SIDE, Math.max(this.MIN_SIDE, Number.isFinite(v) ? Math.round(v) : 1080));
        return { w: c(w), h: c(h) };
    },

    // ── layout (pure apart from ctx.measureText) ─────────────────────────────

    /* Word tokens for the whole text, each with its global character offsets.
     * Paragraphs (newlines) are separate arrays. */
    _tokenize(text) {
        const paras = [];
        let idx = 0;
        String(text).split('\n').forEach(line => {
            const words = [];
            const re = /\S+/g; let m;
            while ((m = re.exec(line))) {
                words.push({ text: m[0], start: idx + m.index, end: idx + m.index + m[0].length });
            }
            paras.push(words);
            idx += line.length + 1; // +1 for the consumed '\n'
        });
        return paras;
    },

    /* Typographic quotes for the card only — the textarea keeps what was typed.
     * One character in, one out, so every range offset still lines up: a quote at
     * the start of a word opens, anywhere else it closes (or is an apostrophe). */
    smartQuotes(text) {
        let out = '';
        for (let i = 0; i < text.length; i++) {
            const ch = text[i];
            if (ch === '"') out += (i === 0) ? '\u201C' : '\u201D';
            else if (ch === "'") out += (i === 0) ? '\u2018' : '\u2019';
            else out += ch;
        }
        return out;
    },

    /* Split a word into segments wherever the highlight record or the style
     * flags change between neighbouring characters. */
    segmentWord(word, highlights, styleMap) {
        const segs = [];
        const hlAt = i => highlights.find(r => i >= r.start && i < r.end) || null;
        const same = (a, b) => a.b === b.b && a.i === b.i && a.u === b.u && a.s === b.s && a.x === b.x;
        let cur = null;
        for (let i = word.start; i < word.end; i++) {
            const hl = hlAt(i);
            const st = styleMap[i] || { b: false, i: false, u: false, s: false, x: false };
            if (cur && cur.hl === hl && same(cur.st, st)) { cur.end = i + 1; continue; }
            if (cur) segs.push(cur);
            cur = { start: i, end: i + 1, hl, st: { b: st.b, i: st.i, u: st.u, s: st.s, x: !!st.x } };
        }
        if (cur) segs.push(cur);
        const text = this.smartQuotes(word.text);
        segs.forEach(sg => { sg.text = text.slice(sg.start - word.start, sg.end - word.start); });
        return segs;
    },

    fontFor(st, px, familyCss) {
        return `${st.i ? 'italic ' : ''}${st.b ? '700' : '400'} ${px}px ${familyCss}`;
    },

    /* Wrap + justify. Returns lines of positioned segments and the card
     * geometry. `ctx` only needs `measureText` (and a settable `font`). */
    layout(ctx, s, dim) {
        const familyCss = (this.FAMILIES[s.font.family] || this.FAMILIES.lora).css;
        const fontPx = s.font.px;
        const lineH = Math.round(fontPx * this.LEADING);
        const indent = Math.round(fontPx * this.INDENT_EM);
        const margin = Math.round(dim.w * 0.055);
        const cardX = margin;
        const cardW = dim.w - margin * 2;
        const pad = Math.round(dim.w * 0.06);
        const textW = cardW - pad * 2;
        const plain = { b: false, i: false, u: false, s: false, x: false };

        ctx.font = this.fontFor(plain, fontPx, familyCss);
        const spaceW = ctx.measureText(' ').width;
        const map = this.styleMap(s.styles, s.text.length);

        // Measure every word as the sum of its segments (each in its own font).
        const paras = this._tokenize(s.text).map(words => words.map(word => {
            const segs = this.segmentWord(word, s.highlights, map);
            let w = 0;
            segs.forEach(sg => {
                ctx.font = this.fontFor(sg.st, fontPx, familyCss);
                sg.w = ctx.measureText(sg.text).width;
                w += sg.w;
            });
            return { ...word, segs, w };
        }));

        const lines = [];
        paras.forEach(words => {
            if (!words.length) { lines.push({ words: [], justify: false, indent: 0 }); return; }
            let cur = [], curW = 0, first = true;
            words.forEach(w => {
                const avail = textW - (first ? indent : 0);
                if (cur.length && curW + spaceW + w.w > avail) {
                    lines.push({ words: cur, justify: true, indent: first ? indent : 0 });
                    cur = []; curW = 0; first = false;
                }
                if (cur.length) curW += spaceW;
                cur.push(w); curW += w.w;
            });
            if (cur.length) lines.push({ words: cur, justify: false, indent: first ? indent : 0 }); // last line ragged
        });

        const textH = lines.length * lineH;
        const cardH = textH + pad * 2;
        const cardY = Math.round((dim.h - cardH) / 2);

        // Position segments: gaps only between words, never inside one.
        let y = cardY + pad;
        lines.forEach(line => {
            line.y = y;
            const gaps = line.words.length - 1;
            const rawW = line.words.reduce((sum, w) => sum + w.w, 0);
            const avail = textW - (line.indent || 0);
            line.gap = (line.justify && gaps > 0) ? (avail - rawW) / gaps : spaceW;
            let x = cardX + pad + (line.indent || 0);
            line.segs = [];
            line.words.forEach((w, wi) => {
                w.segs.forEach(sg => { sg.x = x; sg.wordIndex = wi; line.segs.push(sg); x += sg.w; });
                x += line.gap;
            });
            y += lineH;
        });

        return { lines, fontPx, lineH, indent, familyCss, margin, cardX, cardW, cardH, cardY, pad, textW, spaceW };
    },

    /* Consecutive segments on a line that share `key(seg)` (truthy, by identity
     * or value) become one run spanning from the first's left edge to the
     * last's right edge — including the inter-word gaps in between. */
    runs(line, key) {
        const out = [];
        let cur = null;
        line.segs.forEach(sg => {
            const k = key(sg);
            if (k && cur && cur.key === k) { cur.to = sg.x + sg.w; return; }
            if (cur) out.push(cur);
            cur = k ? { key: k, from: sg.x, to: sg.x + sg.w } : null;
        });
        if (cur) out.push(cur);
        return out;
    },

    // ── draw ─────────────────────────────────────────────────────────────────

    draw() {
        const canvas = document.getElementById('promo-canvas');
        if (!canvas) return;
        const { L, dim } = this.paintCard(canvas, this._state);

        // Overflow guard — warn if the card ran past the canvas.
        const warn = document.getElementById('promo-warn');
        if (warn) warn.textContent = (L.cardH > dim.h - L.margin * 2)
            ? 'Text is taller than the image — shorten it, reduce the text size, or pick a taller size.' : '';
        // Every redraw is a change the person may want to keep (render() resets this after its first paint).
        this._dirty = true;
        this._paintSaveState();
    },

    /* Paint one card (state `s`: shared settings + one page's text and ranges) onto `canvas`. */
    paintCard(canvas, s) {
        const ctx = canvas.getContext('2d');
        const dim = { w: s.size.w, h: s.size.h };
        canvas.width = dim.w;
        canvas.height = dim.h;

        this._drawBackground(ctx, dim, s);
        const L = this.layout(ctx, s, dim);

        // Card with rounded corners + soft shadow; its height fits the text.
        ctx.save();
        ctx.shadowColor = 'rgba(0,0,0,0.22)';
        ctx.shadowBlur = 30;
        ctx.shadowOffsetY = 12;
        ctx.fillStyle = '#ffffff';
        this._roundRect(ctx, L.cardX, L.cardY, L.cardW, L.cardH, 10);
        ctx.fill();
        ctx.restore();

        const fontPx = L.fontPx;
        const ink = '#171717';
        const thick = Math.max(2, Math.round(fontPx * 0.06));
        ctx.textBaseline = 'top';

        L.lines.forEach(line => {
            if (!line.segs || !line.segs.length) return;
            const y = line.y;
            // 1. Colour washes BEHIND the text: one run per highlight record, filling
            //    the whole line box so the blocks of a wrapped run touch (no gaps).
            const boxTop = y - (L.lineH - fontPx) / 2;
            this.runs(line, sg => sg.hl).forEach(r => {
                ctx.fillStyle = r.key.color;
                ctx.fillRect(r.from - 3, boxTop, (r.to - r.from) + 6, L.lineH);
            });
            // 2. The words, each segment in its own face.
            ctx.fillStyle = ink;
            line.segs.forEach(sg => {
                ctx.font = this.fontFor(sg.st, fontPx, L.familyCss);
                ctx.fillText(sg.text, sg.x, y);
            });
            // 3. Decorations: continuous across the spaces of a run, like a browser.
            ctx.fillStyle = ink;
            this.runs(line, sg => sg.st.u).forEach(r => {
                ctx.fillRect(r.from, y + Math.round(fontPx * 1.02), r.to - r.from, thick);
            });
            this.runs(line, sg => sg.st.s).forEach(r => {
                ctx.fillRect(r.from, y + Math.round(fontPx * 0.58), r.to - r.from, thick);
            });
            // 4. Censor OVER the text: a hand-style scribble per run (the reference
            //    card crosses words out rather than boxing them).
            this.runs(line, sg => sg.st.x).forEach(r => {
                this._scribble(ctx, r.from, r.to, y, fontPx);
            });
        });

        // Optional footer/handle strip under the card.
        if (s.footer.trim()) {
            ctx.fillStyle = this._footerColor(s);
            ctx.font = `600 ${Math.round(dim.w * 0.032)}px 'Helvetica Neue', Arial, sans-serif`;
            ctx.textAlign = 'center';
            ctx.textBaseline = 'alphabetic';
            ctx.fillText(s.footer.trim(), dim.w / 2, Math.min(dim.h - L.margin * 0.6, L.cardY + L.cardH + L.margin * 0.9));
            ctx.textAlign = 'left';
        }
        return { L, dim };
    },

    _drawBackground(ctx, dim, s) {
        s = s || this._state;
        if (s.bgImage) {
            // Cover-fit the photo, then darken + blur so text stays legible.
            const img = s.bgImage;
            const scale = Math.max(dim.w / img.width, dim.h / img.height);
            const w = img.width * scale, h = img.height * scale;
            ctx.save();
            try { ctx.filter = 'blur(14px) brightness(0.82)'; } catch (e) { /* older canvas */ }
            ctx.drawImage(img, (dim.w - w) / 2, (dim.h - h) / 2, w, h);
            ctx.restore();
            return;
        }
        const preset = this.BACKGROUNDS[s.bg] || this.BACKGROUNDS.blush;
        const g = ctx.createLinearGradient(0, 0, dim.w, dim.h);
        g.addColorStop(0, preset.stops[0]);
        g.addColorStop(1, preset.stops[1]);
        ctx.fillStyle = g;
        ctx.fillRect(0, 0, dim.w, dim.h);
    },

    // Footer text colour: light on dark backgrounds, dark on light ones.
    _footerColor(s) {
        if (s.bgImage) return 'rgba(255,255,255,0.92)';
        const dark = ['dusk', 'ink'].includes(s.bg);
        return dark ? 'rgba(255,255,255,0.9)' : 'rgba(30,25,35,0.72)';
    },

    /* A deterministic scribble across [x0, x1] over glyphs whose top is y: three
     * zigzag passes, slightly offset, in WHITE — it scratches the word out the way
     * the reference card does (a white scribble on the page, over any highlight). */
    _scribble(ctx, x0, x1, y, fontPx) {
        const cy = y + fontPx * 0.52;
        const amp = fontPx * 0.24;
        const step = Math.max(4, fontPx * 0.22);
        ctx.save();
        ctx.strokeStyle = 'rgba(255,255,255,0.96)';
        ctx.lineWidth = Math.max(2, fontPx * 0.09);
        ctx.lineJoin = 'round';
        ctx.lineCap = 'round';
        for (let pass = 0; pass < 4; pass++) {
            const off = (pass - 1.5) * amp * 0.4;
            ctx.beginPath();
            ctx.moveTo(x0 - 2, cy + off);
            let up = pass % 2 === 0;
            for (let x = x0 - 2 + step; x < x1 + 2 + step; x += step) {
                ctx.lineTo(Math.min(x, x1 + 2), cy + off + (up ? -amp : amp));
                up = !up;
            }
            ctx.stroke();
        }
        ctx.restore();
    },

    _roundRect(ctx, x, y, w, h, r) {
        r = Math.min(r, w / 2, h / 2);
        ctx.beginPath();
        ctx.moveTo(x + r, y);
        ctx.arcTo(x + w, y, x + w, y + h, r);
        ctx.arcTo(x + w, y + h, x, y + h, r);
        ctx.arcTo(x, y + h, x, y, r);
        ctx.arcTo(x, y, x + w, y, r);
        ctx.closePath();
    },
};

if (typeof window !== 'undefined') window.Promo = Promo;
if (typeof module !== 'undefined' && module.exports) module.exports = Promo;
