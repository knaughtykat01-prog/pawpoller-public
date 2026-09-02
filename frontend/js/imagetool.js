/* ── Image Tool (#/imagetool) ─────────────────────────────────────────────
 *
 * A simple, client-side image editor: crop, rotate, resize, censor/blur regions,
 * and re-export in another format — the "tidy this up before I publish it" step
 * that previously meant leaving PawPoller for another app.
 *
 * Everything runs on a <canvas> in the browser: no upload, no server round-trip,
 * no dependency. `_work` is an offscreen canvas holding the CURRENT pixels and is
 * the single source of truth — every operation rewrites it, and `_paint()` just
 * blits it (scaled to fit) onto the visible canvas plus any drag overlay. That
 * keeps edits non-lossy between steps and makes export a straight `_work` dump.
 *
 * Coordinates: the display canvas is scaled to fit its column, so pointer coords
 * are mapped back into WORK pixel space via `_toWork()` before any edit — all
 * operations are defined in real image pixels, never screen pixels.
 *
 * Non-destructive by design: it never overwrites the source artwork. Exports are
 * Download / Send to Posts / Save as a NEW artwork.
 * ──────────────────────────────────────────────────────────────────────── */
window.ImageTool = {

    FORMATS: {
        png: { label: 'PNG (lossless)', mime: 'image/png', ext: 'png' },
        jpeg: { label: 'JPEG (small)', mime: 'image/jpeg', ext: 'jpg' },
        webp: { label: 'WebP (modern)', mime: 'image/webp', ext: 'webp' },
    },

    _work: null,        // offscreen canvas — the current pixels
    _mode: 'crop',      // crop | censor | blur
    _sel: null,         // active drag rect in WORK coords
    _dragging: false,
    _fmt: 'png',
    _quality: 0.9,
    _origName: 'image',
    _undo: [],          // stack of previous _work snapshots (bounded)

    async render() {
        const app = document.getElementById('app');
        app.innerHTML = `
            <div class="page-header">
                <h1>🖼️ Image Tool</h1>
                <p class="muted">Crop, straighten, resize, censor and re-format an image before you publish it.
                Everything happens in your browser — nothing is uploaded until you choose to save.</p>
            </div>
            <div class="itool-layout">
                <div class="itool-controls">
                    <div class="card">
                        <div class="itool-row">
                            <label class="btn btn-sm btn-primary" style="cursor:pointer">📂 Open image
                                <input type="file" id="itool-file" accept="image/png,image/jpeg,image/gif,image/webp" hidden>
                            </label>
                            <button type="button" class="btn btn-sm" id="itool-from-lib">🗂️ From library</button>
                        </div>
                        <div class="itool-hint muted" id="itool-meta">No image open yet.</div>
                    </div>

                    <div class="card" id="itool-tools" hidden>
                        <div class="field">Tool
                            <div class="itool-modes" role="group">
                                <button type="button" class="itool-mode is-active" data-mode="crop">⬚ Crop</button>
                                <button type="button" class="itool-mode" data-mode="censor">⬛ Censor</button>
                                <button type="button" class="itool-mode" data-mode="blur">▨ Blur</button>
                            </div>
                        </div>
                        <div class="itool-hint muted" id="itool-tip">Drag a box on the image, then Apply.</div>
                        <div class="itool-row">
                            <button type="button" class="btn btn-sm btn-primary" id="itool-apply">Apply</button>
                            <button type="button" class="btn btn-sm" id="itool-clearsel">Clear box</button>
                            <button type="button" class="btn btn-sm" id="itool-undo" disabled>↶ Undo</button>
                        </div>
                        <hr class="itool-hr">
                        <div class="itool-row">
                            <button type="button" class="btn btn-sm" id="itool-rot-l">↺ 90°</button>
                            <button type="button" class="btn btn-sm" id="itool-rot-r">↻ 90°</button>
                            <button type="button" class="btn btn-sm" id="itool-flip">⇋ Flip</button>
                        </div>
                        <hr class="itool-hr">
                        <label class="field">Resize — longest edge
                            <div class="itool-row">
                                <input type="number" id="itool-edge" class="itool-input" min="64" max="8000" step="1">
                                <button type="button" class="btn btn-sm" id="itool-resize">Resize</button>
                            </div>
                        </label>
                    </div>

                    <div class="card" id="itool-export" hidden>
                        <label class="field">Format
                            <select id="itool-fmt" class="itool-input">${Object.entries(this.FORMATS).map(([k, v]) =>
                                `<option value="${k}"${k === this._fmt ? ' selected' : ''}>${v.label}</option>`).join('')}
                            </select>
                        </label>
                        <label class="field" id="itool-qwrap" hidden>Quality <span id="itool-qval" class="muted"></span>
                            <input type="range" id="itool-quality" min="0.4" max="1" step="0.05" value="${this._quality}">
                        </label>
                        <div class="itool-row">
                            <button class="btn btn-primary btn-sm" id="itool-download">⬇ Download</button>
                            <button class="btn btn-sm" id="itool-toposts">💬 Send to Posts</button>
                            <button class="btn btn-sm" id="itool-save">＋ Save as artwork</button>
                        </div>
                        <div class="itool-hint muted" id="itool-msg"></div>
                    </div>
                </div>

                <div class="itool-stage">
                    <canvas id="itool-canvas" class="itool-canvas"></canvas>
                    <div class="itool-empty" id="itool-empty">Open an image to start</div>
                </div>
            </div>`;
        this._wire();
    },

    _wire() {
        const $ = id => document.getElementById(id);

        $('itool-file').addEventListener('change', e => {
            const f = e.target.files && e.target.files[0];
            if (f) this._loadFile(f);
        });
        $('itool-from-lib').addEventListener('click', () => this._openLibrary());

        document.querySelectorAll('.itool-mode').forEach(b =>
            b.addEventListener('click', () => {
                this._mode = b.dataset.mode;
                document.querySelectorAll('.itool-mode').forEach(x => x.classList.toggle('is-active', x === b));
                $('itool-tip').textContent = this._mode === 'crop'
                    ? 'Drag a box on the image, then Apply to crop to it.'
                    : this._mode === 'censor'
                        ? 'Drag over anything you want blacked out, then Apply.'
                        : 'Drag over anything you want pixelated, then Apply.';
                this._paint();
            }));

        $('itool-apply').addEventListener('click', () => this._apply());
        $('itool-clearsel').addEventListener('click', () => { this._sel = null; this._paint(); });
        $('itool-undo').addEventListener('click', () => this._undoLast());
        $('itool-rot-l').addEventListener('click', () => this._rotate(-90));
        $('itool-rot-r').addEventListener('click', () => this._rotate(90));
        $('itool-flip').addEventListener('click', () => this._flip());
        $('itool-resize').addEventListener('click', () => this._resize());

        const fmt = $('itool-fmt');
        fmt.addEventListener('change', () => { this._fmt = fmt.value; this._syncQuality(); });
        const q = $('itool-quality');
        q.addEventListener('input', () => { this._quality = parseFloat(q.value); this._syncQuality(); });

        $('itool-download').addEventListener('click', () => this._download());
        $('itool-toposts').addEventListener('click', () => this._sendToPosts());
        $('itool-save').addEventListener('click', () => this._saveAsArtwork());

        this._wireCanvas();
    },

    _syncQuality() {
        const lossy = this._fmt !== 'png';
        document.getElementById('itool-qwrap').hidden = !lossy;
        document.getElementById('itool-qval').textContent = lossy
            ? `${Math.round(this._quality * 100)}%` : '';
    },

    /* ── Loading ─────────────────────────────────────────────── */

    _loadFile(file) {
        const url = URL.createObjectURL(file);
        this._origName = (file.name || 'image').replace(/\.[^.]+$/, '');
        this._loadUrl(url, () => URL.revokeObjectURL(url));
    },

    _loadUrl(url, done) {
        const img = new Image();
        img.crossOrigin = 'anonymous';   // keep the canvas untainted where allowed
        img.onload = () => {
            const c = document.createElement('canvas');
            c.width = img.naturalWidth; c.height = img.naturalHeight;
            c.getContext('2d').drawImage(img, 0, 0);
            this._work = c;
            this._sel = null;
            this._undo = [];
            document.getElementById('itool-tools').hidden = false;
            document.getElementById('itool-export').hidden = false;
            document.getElementById('itool-empty').hidden = true;
            document.getElementById('itool-edge').value = Math.max(c.width, c.height);
            this._syncQuality();
            this._paint();
            if (done) done();
        };
        img.onerror = () => {
            this._msg('Could not open that image.');
            if (done) done();
        };
        img.src = url;
    },

    /* Pick an existing artwork from the library to edit (non-destructively). */
    async _openLibrary() {
        let list = [];
        try {
            const d = await API.getArtworks();
            list = (d && d.artworks) || [];
        } catch (e) { this._msg('Could not load your library.'); return; }
        if (!list.length) { this._msg('No artwork in your library yet.'); return; }

        let ov = document.getElementById('itool-lib-ov');
        if (!ov) {
            ov = document.createElement('div');
            ov.id = 'itool-lib-ov';
            ov.className = 'promo-ov';   // reuse the Promo Maker's overlay styling
            document.body.appendChild(ov);
        }
        ov.innerHTML = `
            <div class="promo-ovbox">
                <h3>Open from your library</h3>
                <p class="muted">Editing never changes the original — save the result as a new artwork.</p>
                <div class="itool-lib">${list.filter(a => a.image).map(a =>
                    `<button type="button" class="itool-libcard" data-name="${Utils.escapeHtml(a.name)}"
                        data-file="${Utils.escapeHtml(a.image)}">
                        <img src="/api/artwork/image?name=${encodeURIComponent(a.name)}&file=${encodeURIComponent(a.image)}"
                            alt="" loading="lazy">
                        <span>${Utils.escapeHtml(a.title || a.name)}</span>
                    </button>`).join('')}</div>
                <div class="promo-ov-actions"><button class="btn btn-sm" id="itool-lib-close">Cancel</button></div>
            </div>`;
        ov.classList.add('open');
        const close = () => ov.classList.remove('open');
        document.getElementById('itool-lib-close').onclick = close;
        ov.onclick = e => { if (e.target === ov) close(); };
        ov.querySelectorAll('.itool-libcard').forEach(b => b.addEventListener('click', () => {
            this._origName = b.dataset.name;
            this._loadUrl(`/api/artwork/image?name=${encodeURIComponent(b.dataset.name)}`
                + `&file=${encodeURIComponent(b.dataset.file)}`);
            close();
        }));
    },

    /* ── Painting + pointer mapping ──────────────────────────── */

    _paint() {
        const cv = document.getElementById('itool-canvas');
        if (!cv || !this._work) return;
        // Fit the work image into a sane on-screen size; export always uses _work
        // at full resolution, so this scale is purely cosmetic.
        const maxW = Math.min(cv.parentElement.clientWidth - 32, 1100) || 800;
        const maxH = Math.round(window.innerHeight * 0.72);
        const scale = Math.min(1, maxW / this._work.width, maxH / this._work.height);
        cv.width = Math.max(1, Math.round(this._work.width * scale));
        cv.height = Math.max(1, Math.round(this._work.height * scale));
        cv._scale = scale;
        const ctx = cv.getContext('2d');
        ctx.clearRect(0, 0, cv.width, cv.height);
        ctx.drawImage(this._work, 0, 0, cv.width, cv.height);

        if (this._sel) {
            const s = this._sel, k = scale;
            ctx.save();
            if (this._mode === 'crop') {
                // Dim everything outside the crop box so the keep-area reads clearly.
                ctx.fillStyle = 'rgba(0,0,0,0.45)';
                ctx.fillRect(0, 0, cv.width, cv.height);
                ctx.clearRect(s.x * k, s.y * k, s.w * k, s.h * k);
                ctx.drawImage(this._work, s.x, s.y, s.w, s.h, s.x * k, s.y * k, s.w * k, s.h * k);
            } else {
                ctx.fillStyle = 'rgba(255,255,255,0.25)';
                ctx.fillRect(s.x * k, s.y * k, s.w * k, s.h * k);
            }
            ctx.strokeStyle = '#fff';
            ctx.lineWidth = 1.5;
            ctx.setLineDash([5, 4]);
            ctx.strokeRect(s.x * k, s.y * k, s.w * k, s.h * k);
            ctx.restore();
        }
        const m = document.getElementById('itool-meta');
        if (m) m.textContent = `${this._work.width} × ${this._work.height} px`;
    },

    /* Pointer → WORK pixel coords (the canvas is displayed scaled). */
    _toWork(e, cv) {
        const r = cv.getBoundingClientRect();
        const k = cv._scale || 1;
        // getBoundingClientRect can differ from canvas.width if CSS resizes it,
        // so normalise through the rect rather than trusting _scale alone.
        const x = (e.clientX - r.left) * (cv.width / r.width) / k;
        const y = (e.clientY - r.top) * (cv.height / r.height) / k;
        return {
            x: Math.max(0, Math.min(this._work.width, x)),
            y: Math.max(0, Math.min(this._work.height, y)),
        };
    },

    _wireCanvas() {
        const cv = document.getElementById('itool-canvas');
        if (!cv) return;
        let anchor = null;
        const down = e => {
            if (!this._work) return;
            e.preventDefault();
            anchor = this._toWork(e, cv);
            this._dragging = true;
            this._sel = { x: anchor.x, y: anchor.y, w: 0, h: 0 };
        };
        const move = e => {
            if (!this._dragging || !anchor) return;
            const p = this._toWork(e, cv);
            this._sel = {
                x: Math.min(anchor.x, p.x), y: Math.min(anchor.y, p.y),
                w: Math.abs(p.x - anchor.x), h: Math.abs(p.y - anchor.y),
            };
            this._paint();
        };
        const up = () => {
            this._dragging = false;
            if (this._sel && (this._sel.w < 3 || this._sel.h < 3)) this._sel = null;
            this._paint();
        };
        cv.addEventListener('pointerdown', down);
        window.addEventListener('pointermove', move);
        window.addEventListener('pointerup', up);
    },

    /* ── Edits (each pushes an undo snapshot) ────────────────── */

    _snapshot() {
        if (!this._work) return;
        const c = document.createElement('canvas');
        c.width = this._work.width; c.height = this._work.height;
        c.getContext('2d').drawImage(this._work, 0, 0);
        this._undo.push(c);
        if (this._undo.length > 12) this._undo.shift();   // bound the memory
        document.getElementById('itool-undo').disabled = false;
    },

    _undoLast() {
        const prev = this._undo.pop();
        if (!prev) return;
        this._work = prev;
        this._sel = null;
        document.getElementById('itool-undo').disabled = !this._undo.length;
        document.getElementById('itool-edge').value = Math.max(this._work.width, this._work.height);
        this._paint();
    },

    _apply() {
        if (!this._work) return;
        if (!this._sel) { this._msg('Drag a box on the image first.'); return; }
        const s = {
            x: Math.round(this._sel.x), y: Math.round(this._sel.y),
            w: Math.round(this._sel.w), h: Math.round(this._sel.h),
        };
        if (s.w < 2 || s.h < 2) { this._msg('That box is too small.'); return; }
        this._snapshot();
        if (this._mode === 'crop') this._crop(s);
        else if (this._mode === 'censor') this._censor(s);
        else this._pixelate(s);
        this._sel = null;
        document.getElementById('itool-edge').value = Math.max(this._work.width, this._work.height);
        this._paint();
        this._msg('');
    },

    _crop(s) {
        const c = document.createElement('canvas');
        c.width = s.w; c.height = s.h;
        c.getContext('2d').drawImage(this._work, s.x, s.y, s.w, s.h, 0, 0, s.w, s.h);
        this._work = c;
    },

    _censor(s) {
        const ctx = this._work.getContext('2d');
        ctx.fillStyle = '#000';
        ctx.fillRect(s.x, s.y, s.w, s.h);
    },

    /* Pixelate = downscale the region then blow it back up with smoothing off. */
    _pixelate(s, block = 14) {
        const ctx = this._work.getContext('2d');
        const tw = Math.max(1, Math.round(s.w / block));
        const th = Math.max(1, Math.round(s.h / block));
        const tmp = document.createElement('canvas');
        tmp.width = tw; tmp.height = th;
        const tctx = tmp.getContext('2d');
        tctx.imageSmoothingEnabled = false;
        tctx.drawImage(this._work, s.x, s.y, s.w, s.h, 0, 0, tw, th);
        ctx.imageSmoothingEnabled = false;
        ctx.drawImage(tmp, 0, 0, tw, th, s.x, s.y, s.w, s.h);
        ctx.imageSmoothingEnabled = true;
    },

    _rotate(deg) {
        if (!this._work) return;
        this._snapshot();
        const w = this._work.width, h = this._work.height;
        const c = document.createElement('canvas');
        c.width = h; c.height = w;                 // 90° swaps the axes
        const ctx = c.getContext('2d');
        ctx.translate(c.width / 2, c.height / 2);
        ctx.rotate(deg * Math.PI / 180);
        ctx.drawImage(this._work, -w / 2, -h / 2);
        this._work = c;
        this._sel = null;
        document.getElementById('itool-edge').value = Math.max(c.width, c.height);
        this._paint();
    },

    _flip() {
        if (!this._work) return;
        this._snapshot();
        const c = document.createElement('canvas');
        c.width = this._work.width; c.height = this._work.height;
        const ctx = c.getContext('2d');
        ctx.translate(c.width, 0);
        ctx.scale(-1, 1);
        ctx.drawImage(this._work, 0, 0);
        this._work = c;
        this._sel = null;
        this._paint();
    },

    _resize() {
        if (!this._work) return;
        const edge = parseInt(document.getElementById('itool-edge').value, 10);
        if (!edge || edge < 16) { this._msg('Enter a longest-edge size (px).'); return; }
        const cur = Math.max(this._work.width, this._work.height);
        if (edge === cur) return;
        this._snapshot();
        const k = edge / cur;
        const c = document.createElement('canvas');
        c.width = Math.max(1, Math.round(this._work.width * k));
        c.height = Math.max(1, Math.round(this._work.height * k));
        const ctx = c.getContext('2d');
        ctx.imageSmoothingEnabled = true;
        ctx.imageSmoothingQuality = 'high';
        ctx.drawImage(this._work, 0, 0, c.width, c.height);
        this._work = c;
        this._sel = null;
        this._paint();
        this._msg(`Resized to ${c.width} × ${c.height}.`);
    },

    /* ── Export ──────────────────────────────────────────────── */

    _blob() {
        const f = this.FORMATS[this._fmt] || this.FORMATS.png;
        return new Promise(res => this._work.toBlob(
            b => res({ blob: b, fmt: f }), f.mime,
            this._fmt === 'png' ? undefined : this._quality));
    },

    async _download() {
        if (!this._work) return;
        const { blob, fmt } = await this._blob();
        if (!blob) { this._msg('Export failed.'); return; }
        const url = URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.href = url;
        a.download = `${this._origName}-edited.${fmt.ext}`;
        document.body.appendChild(a); a.click(); a.remove();
        setTimeout(() => URL.revokeObjectURL(url), 1000);
    },

    async _sendToPosts() {
        if (!this._work || !window.Posts) return;
        const { blob, fmt } = await this._blob();
        if (!blob) return;
        window.Posts._handoffFiles = [
            new File([blob], `${this._origName}-edited.${fmt.ext}`, { type: fmt.mime })];
        window.location.hash = '#/posts/new';
    },

    /* Save the edited result as a NEW artwork — the original is never touched. */
    async _saveAsArtwork() {
        if (!this._work) return;
        const title = window.prompt('Title for the new artwork:', `${this._origName} (edited)`);
        if (!title) return;
        const { blob, fmt } = await this._blob();
        if (!blob) { this._msg('Export failed.'); return; }
        const file = new File([blob], `${this._origName}-edited.${fmt.ext}`, { type: fmt.mime });
        this._msg('Saving…');
        try {
            const r = await API.uploadArtwork(file, { title, tags: {} }, null);
            this._msg('');
            if (window.toast && toast.success) toast.success('Saved to your library');
            window.location.hash = `#/artwork/image/${encodeURIComponent(r.name)}`;
        } catch (err) {
            this._msg('Save failed: ' + (err.message || err));
        }
    },

    _msg(t) {
        const el = document.getElementById('itool-msg');
        if (el) el.textContent = t || '';
    },
};
