/* ── Promo Maker (#/promo) ───────────────────────────────────────────────
 *
 * A client-side "BookTok"-style promotional-image generator: paste an excerpt,
 * highlight the spicy phrases in colour, drop it on a background, and export a
 * social-ready PNG (square / portrait / story). Everything runs in-browser on a
 * <canvas> — no server round-trip, no font dependency, works identically on the
 * desktop app and the server. Modelled on the viral book-excerpt cards (see the
 * reference the feature was built from: a serif page with pastel highlights).
 *
 * Data model: the excerpt lives in a <textarea>; highlights are stored as a list
 * of { start, end, color } character ranges against that text. The user selects
 * text and clicks a swatch to add a range; draw() word-wraps + justifies the
 * text on the canvas and paints a coloured rect behind any word whose characters
 * fall inside a highlight range.
 * ──────────────────────────────────────────────────────────────────────── */
window.Promo = {

    // Canvas size presets (social aspect ratios). Rendered at half-ish res for
    // snappy preview, exported 1:1 — the canvas IS the export, so these are the
    // real pixel dimensions.
    SIZES: {
        square: { w: 1080, h: 1080, label: 'Square 1:1' },
        portrait: { w: 1080, h: 1350, label: 'Portrait 4:5' },
        story: { w: 1080, h: 1920, label: 'Story 9:16' },
    },

    // Highlighter palette — soft pastels that read under dark serif text.
    COLORS: ['#f7b6d2', '#ffd9a0', '#a0e6b4', '#a8dcf0', '#d6b8f0', '#f5e79e'],

    // Background presets (CSS-ish; drawn as canvas gradients / solids).
    BACKGROUNDS: {
        blush: { label: 'Blush', stops: ['#f6d5d0', '#c9a7c8'] },
        dusk: { label: 'Dusk', stops: ['#2b2140', '#4b3a63'] },
        ink: { label: 'Ink', stops: ['#14161c', '#242a38'] },
        sage: { label: 'Sage', stops: ['#d7e6cf', '#a7c4a0'] },
        peach: { label: 'Peach', stops: ['#ffe3c2', '#f3b7a6'] },
        white: { label: 'Plain', stops: ['#ececf0', '#dcdce4'] },
    },

    _state: null,

    render() {
        const app = document.getElementById('app');
        // Sensible default so the tool is never a blank canvas on first open.
        const sample = '"What?"\n"I don\'t have any idea what you mean."\n'
            + 'She smirks, and something in the room shifts. Slowly, she leans in, '
            + 'her voice dropping to almost nothing. "You know exactly what I mean," '
            + 'she says. "Right where it belongs."';

        this._state = this._state || {
            text: sample,
            highlights: [],
            size: 'portrait',
            bg: 'blush',
            font: 60,           // font size in canvas px (tuned so the sample fits)
            serif: true,
            footer: '',
            bgImage: null,      // optional uploaded Image()
            color: this.COLORS[0],
        };
        const s = this._state;

        const swatches = this.COLORS.map(c =>
            `<button type="button" class="promo-swatch${c === s.color ? ' is-active' : ''}" `
            + `data-color="${c}" style="background:${c}" title="Highlight in this colour"></button>`).join('');
        const sizeOpts = Object.entries(this.SIZES).map(([k, v]) =>
            `<option value="${k}"${k === s.size ? ' selected' : ''}>${v.label}</option>`).join('');
        const bgSwatches = Object.entries(this.BACKGROUNDS).map(([k, v]) =>
            `<button type="button" class="promo-bg${k === s.bg ? ' is-active' : ''}" data-bg="${k}" `
            + `style="background:linear-gradient(135deg,${v.stops[0]},${v.stops[1]})" title="${v.label}"></button>`).join('');

        app.innerHTML = `
            <div class="page-header">
                <h1>✨ Promo Maker</h1>
                <p class="muted">Turn a spicy excerpt into a shareable image. Paste your text, select a phrase and
                tap a colour to highlight it, pick a background and size, then download. Great for Instagram, TikTok
                covers, Bluesky and Threads.</p>
            </div>
            <div class="promo-layout">
                <div class="promo-controls">
                    <div class="card">
                        <div class="promo-src-row">
                            <button type="button" class="btn btn-sm" id="promo-from-story">📖 Pull from a story</button>
                            <span class="muted">or paste your own below</span>
                        </div>
                        <label class="field">Excerpt
                            <textarea id="promo-text" rows="8" spellcheck="false"
                                placeholder="Paste a passage from your story…">${Utils.escapeHtml(s.text)}</textarea>
                        </label>
                        <div class="promo-hint muted">Select some words above, then tap a colour to highlight them.</div>
                        <div class="promo-swatches">${swatches}
                            <button type="button" class="promo-swatch promo-swatch--censor" id="promo-censor"
                                title="Black out the selected words (censor bar)"></button>
                            <button type="button" class="btn btn-sm" id="promo-clearhl">Clear</button>
                        </div>
                    </div>
                    <div class="card">
                        <div class="field-row">
                            <label class="field">Size
                                <select id="promo-size">${sizeOpts}</select>
                            </label>
                            <label class="field">Text size
                                <input type="range" id="promo-font" min="48" max="140" step="2" value="${s.font}">
                            </label>
                        </div>
                        <label class="promo-check"><input type="checkbox" id="promo-serif"${s.serif ? ' checked' : ''}> Serif font (book look)</label>
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
                    </div>
                    <div class="promo-actions">
                        <button class="btn btn-primary" id="promo-download">⬇ Download PNG</button>
                        <button class="btn" id="promo-share" title="Open the post composer with this image attached">💬 Send to Posts</button>
                        <span id="promo-warn" class="promo-warn"></span>
                    </div>
                </div>
                <div class="promo-preview">
                    <canvas id="promo-canvas"></canvas>
                </div>
            </div>`;

        this._wire();
        this.draw();
    },

    _wire() {
        const s = this._state;
        const $ = id => document.getElementById(id);

        $('promo-text').addEventListener('input', e => {
            // Text changed — character offsets shift, so old highlights no longer
            // map cleanly. Keep those that still fit within the new length.
            s.text = e.target.value;
            s.highlights = s.highlights.filter(h => h.end <= s.text.length);
            this.draw();
        });

        document.querySelectorAll('.promo-swatch').forEach(b => {
            // Prevent the swatch from stealing focus — otherwise the click blurs
            // the textarea and some browsers collapse its selection before we can
            // read it. mousedown-preventDefault keeps the selection alive.
            b.addEventListener('mousedown', e => e.preventDefault());
            b.addEventListener('click', () => {
                s.color = b.dataset.color;
                document.querySelectorAll('.promo-swatch').forEach(x => x.classList.toggle('is-active', x === b));
                this._applyHighlight();
            });
        });

        $('promo-clearhl').addEventListener('click', () => { s.highlights = []; this.draw(); });
        // Censor: same selection flow as a colour swatch, but flagged as a blackout.
        const censor = $('promo-censor');
        censor.addEventListener('mousedown', e => e.preventDefault());   // keep the textarea selection
        censor.addEventListener('click', () => this._applyHighlight(true));

        $('promo-size').addEventListener('change', e => { s.size = e.target.value; this.draw(); });
        $('promo-font').addEventListener('input', e => { s.font = parseInt(e.target.value, 10); this.draw(); });
        $('promo-serif').addEventListener('change', e => { s.serif = e.target.checked; this.draw(); });
        $('promo-footer').addEventListener('input', e => { s.footer = e.target.value; this.draw(); });

        document.querySelectorAll('.promo-bg').forEach(b =>
            b.addEventListener('click', () => {
                s.bg = b.dataset.bg;
                s.bgImage = null;
                $('promo-bgclear').disabled = true;
                document.querySelectorAll('.promo-bg').forEach(x => x.classList.toggle('is-active', x === b));
                this.draw();
            }));

        $('promo-bgimg').addEventListener('change', e => {
            const file = e.target.files && e.target.files[0];
            if (!file) return;
            const img = new Image();
            img.onload = () => { s.bgImage = img; $('promo-bgclear').disabled = false; this.draw(); };
            img.src = URL.createObjectURL(file);
        });
        $('promo-bgclear').addEventListener('click', () => {
            s.bgImage = null; $('promo-bgclear').disabled = true; this.draw();
        });

        $('promo-download').addEventListener('click', () => this._download());
        $('promo-share').addEventListener('click', () => this._shareToPosts());
        $('promo-from-story').addEventListener('click', () => this._openStoryPicker());
    },

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
        canvas.toBlob(blob => {
            if (!blob) return;
            const file = new File([blob], `pawpoller-promo-${Date.now()}.png`, { type: 'image/png' });
            // Picked up by Posts.renderCompose() once the composer has rendered.
            window.Posts._handoffFiles = [file];
            window.location.hash = '#/posts/new';
        }, 'image/png');
    },

    /* ── "Pull from a story" picker ─────────────────────────────
     * Loads a story's MASTER.md via the editor API, shows it read-only, and
     * lifts whatever the user selects into the excerpt box. Markdown/metadata is
     * stripped so the canvas renders clean prose. */
    async _openStoryPicker() {
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
        const close = () => ov.classList.remove('open');
        document.getElementById('promo-story-close').onclick = close;
        ov.onclick = e => { if (e.target === ov) close(); };

        // Story chosen via the visual WorkPicker (2.162.0) — replaces the old
        // dropdown. Single-select, stories only; on confirm we load that story's
        // text into the read-only pane for the user to lift a passage from.
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

        document.getElementById('promo-use-sel').onclick = () => {
            const chosen = src.value.slice(src.selectionStart, src.selectionEnd).trim();
            if (!chosen) { msg.textContent = 'Select some text in the story first.'; return; }
            const s = this._state;
            s.text = chosen;
            s.highlights = [];   // character offsets no longer map to the new text
            document.getElementById('promo-text').value = chosen;
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

    /* Turn the current textarea selection into a highlight range in the active
     * colour. Overlapping ranges of the same colour are merged; a fresh colour
     * over an existing highlight wins (last-write). */
    _applyHighlight(censor = false) {
        const ta = document.getElementById('promo-text');
        const start = ta.selectionStart, end = ta.selectionEnd;
        if (start === end) {  // nothing selected — just switched colour
            return;
        }
        const s = this._state;
        // Drop any existing highlight fully covered by the new one, and trim
        // partial overlaps so colours don't stack ambiguously.
        s.highlights = s.highlights.filter(h => h.end <= start || h.start >= end);
        s.highlights.push(censor
            ? { start, end, color: '#12100f', censor: true }
            : { start, end, color: s.color });
        this.draw();
    },

    /* Word tokens for the whole text, each with its global character offsets so
     * we can test membership in a highlight range. Paragraphs (newlines) are
     * preserved as an explicit break flag on the first word of each line. */
    _tokenize(text) {
        const paras = [];
        let idx = 0;
        text.split('\n').forEach(line => {
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

    /* The highlight record covering this word (or null). A record with
     * `censor: true` is a blackout bar rather than a colour wash. */
    _hlFor(word) {
        return this._state.highlights.find(
            r => word.start < r.end && word.end > r.start) || null;
    },

    draw() {
        const s = this._state;
        const canvas = document.getElementById('promo-canvas');
        if (!canvas) return;
        const ctx = canvas.getContext('2d');
        const dim = this.SIZES[s.size];
        canvas.width = dim.w;
        canvas.height = dim.h;

        this._drawBackground(ctx, dim);

        // The white "page" card: a fixed inset with generous inner padding. Its
        // height auto-fits the wrapped text so it always looks centred.
        const margin = Math.round(dim.w * 0.055);
        const cardX = margin;
        const cardW = dim.w - margin * 2;
        const pad = Math.round(dim.w * 0.06);
        const textW = cardW - pad * 2;

        const family = s.serif ? "Georgia, 'Times New Roman', serif" : "'Helvetica Neue', Arial, sans-serif";
        const fontPx = s.font;
        const lineH = Math.round(fontPx * 1.5);
        ctx.font = `${fontPx}px ${family}`;
        ctx.textBaseline = 'top';

        // Word-wrap every paragraph into lines of {words, widths, justify}.
        const paras = this._tokenize(s.text);
        const spaceW = ctx.measureText(' ').width;
        const lines = [];
        paras.forEach(words => {
            if (!words.length) { lines.push({ words: [], justify: false }); return; }
            let cur = [], curW = 0;
            words.forEach(w => {
                const ww = ctx.measureText(w.text).width;
                if (cur.length && curW + spaceW + ww > textW) {
                    lines.push({ words: cur, justify: true });
                    cur = []; curW = 0;
                }
                if (cur.length) curW += spaceW;
                cur.push(w); curW += ww;
            });
            if (cur.length) lines.push({ words: cur, justify: false }); // last line ragged
        });

        const textH = lines.length * lineH;
        const cardH = textH + pad * 2;
        const cardY = Math.round((dim.h - cardH) / 2);

        // Card with rounded corners + soft shadow.
        ctx.save();
        ctx.shadowColor = 'rgba(0,0,0,0.28)';
        ctx.shadowBlur = 40;
        ctx.shadowOffsetY = 18;
        ctx.fillStyle = '#ffffff';
        this._roundRect(ctx, cardX, cardY, cardW, cardH, 28);
        ctx.fill();
        ctx.restore();

        // Draw the text, painting highlight rects behind highlighted words.
        ctx.fillStyle = '#171717';
        ctx.font = `${fontPx}px ${family}`;
        ctx.textBaseline = 'top';
        let y = cardY + pad;
        lines.forEach(line => {
            const x0 = cardX + pad;
            if (!line.words.length) { y += lineH; return; }
            // Justification: spread the slack across inter-word gaps (skip last
            // line of a paragraph and single-word lines so they stay natural).
            const rawW = line.words.reduce((sum, w) => sum + ctx.measureText(w.text).width, 0);
            const gaps = line.words.length - 1;
            let gap = spaceW;
            if (line.justify && gaps > 0) gap = (textW - rawW) / gaps;
            let x = x0;
            line.words.forEach(w => {
                const ww = ctx.measureText(w.text).width;
                const hl = this._hlFor(w);
                // Colour wash goes BEHIND the words; a censor bar goes OVER them.
                if (hl && !hl.censor) {
                    ctx.fillStyle = hl.color;
                    ctx.fillRect(x - 4, y + Math.round(fontPx * 0.06), ww + 8, Math.round(fontPx * 1.12));
                }
                ctx.fillStyle = '#171717';
                ctx.fillText(w.text, x, y);
                if (hl && hl.censor) {
                    ctx.fillStyle = '#12100f';
                    ctx.fillRect(x - 3, y + Math.round(fontPx * 0.14), ww + 6, Math.round(fontPx * 0.92));
                }
                x += ww + gap;
            });
            y += lineH;
        });

        // Optional footer/handle strip under the card.
        if (s.footer.trim()) {
            ctx.fillStyle = this._footerColor(s);
            ctx.font = `600 ${Math.round(dim.w * 0.032)}px 'Helvetica Neue', Arial, sans-serif`;
            ctx.textAlign = 'center';
            ctx.textBaseline = 'alphabetic';
            ctx.fillText(s.footer.trim(), dim.w / 2, Math.min(dim.h - margin * 0.6, cardY + cardH + margin * 0.9));
            ctx.textAlign = 'left';
        }

        // Overflow guard — warn if the card ran past the canvas.
        const warn = document.getElementById('promo-warn');
        if (warn) warn.textContent = (cardH > dim.h - margin * 2)
            ? 'Text is taller than the image — shorten it or reduce the text size.' : '';
    },

    _drawBackground(ctx, dim) {
        const s = this._state;
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
};
