/* ── Media kinds (MEDIATYPES, 4.18.0) ─────────────────────────────────────────
 *
 * The browser half of posting/media_kinds.py: which extensions are images,
 * video or audio, and — because the server never decodes media (no ffmpeg
 * ships) — the measuring that happens HERE at upload time: duration and
 * dimensions from a <video>/<audio> element, and the poster every video/audio
 * piece must carry: a frame at min(1 s, duration/2) drawn to a canvas for
 * video, a peak-envelope waveform from WebAudio for audio. Both deterministic
 * for a given file. The person can swap the poster for any image afterwards.
 * ───────────────────────────────────────────────────────────────────────── */
(function () {
    const IMAGE = ['png', 'jpg', 'jpeg', 'gif', 'webp'];
    const VIDEO = ['mp4', 'webm', 'mov', 'm4v'];
    const AUDIO = ['mp3', 'wav', 'flac', 'ogg', 'm4a', 'aac', 'opus'];

    const MediaKinds = {
        IMAGE, VIDEO, AUDIO,
        ACCEPT: [...IMAGE, ...VIDEO, ...AUDIO].map(e => '.' + e).join(','),
        IMAGE_ACCEPT: 'image/png,image/jpeg,image/gif,image/webp',
        HINT: 'PNG, JPG, GIF, WebP · MP4, WebM, MOV · MP3, WAV, FLAC, OGG',
        POSTER_MAX: 1600,
        _support: null,

        extOf(name) {
            const m = /\.([a-z0-9]+)$/i.exec(String(name || ''));
            return m ? m[1].toLowerCase() : '';
        },
        kindOf(name) {
            const e = this.extOf(name);
            if (IMAGE.includes(e)) return 'image';
            if (VIDEO.includes(e)) return 'video';
            if (AUDIO.includes(e)) return 'audio';
            return null;
        },
        fmtDuration(s) {
            s = Math.round(Number(s) || 0);
            if (s < 0) return '';
            const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), sec = s % 60;
            const mm = h ? String(m).padStart(2, '0') : String(m);
            return (h ? h + ':' : '') + mm + ':' + String(sec).padStart(2, '0');
        },
        /* '▶ 0:42' / '♪ 3:10' / '' for an image. */
        badge(media, kind) {
            const k = kind || (media && media.kind);
            if (k !== 'video' && k !== 'audio') return '';
            const d = media && media.duration_s != null ? this.fmtDuration(media.duration_s) : '';
            return (k === 'video' ? '▶' : '♪') + (d ? ' ' + d : '');
        },
        /* The per-site capability table (GET /api/platforms/media), fetched once. */
        async support() {
            if (this._support) return this._support;
            try {
                this._support = await API.get('/api/platforms/media');
            } catch (e) {
                this._support = { platforms: {} };
            }
            return this._support;
        },
        /* 4.21.0: the three-step rating ladder (mirrors posting/platforms/base.py rating_rank). */
        RATING_WORD: ['general', 'mature', 'adult'],
        ratingRank(r) {
            const k = String(r || '').trim().toLowerCase();
            if (['general', 'safe', 'sfw', 'g', 's', 'e', 'everyone', ''].includes(k)) return 0;
            if (['mature', 'questionable', 'm', 'q', 'teen', 't'].includes(k)) return 1;
            return 2;
        },
        /* Does site `code` take this kind + extension at this rating? {ok, reason} (reason = the site's own sentence).
         * `kind` may be null (no file picked yet): only the rating is checked then. */
        acceptance(support, code, kind, ext, rating) {
            const p = support && support.platforms && support.platforms[code];
            if (!p) return { ok: true, reason: '' };
            const byCode = (typeof window !== 'undefined' && window.platformByCode) ? window.platformByCode : null;
            const label = (byCode && byCode(code) || {}).label || code;
            if (kind) {
                const list = (p.accepts && p.accepts[kind]) || [];
                if (!list.map(x => String(x).toLowerCase()).includes(String(ext || '').toLowerCase())) {
                    return { ok: false, reason: `${label} doesn't take ${ext ? ext + ' ' : ''}${kind} — it takes ${p.label || 'other kinds'}.` };
                }
            }
            if (rating != null && p.max_rating) {
                const have = this.ratingRank(rating), allowed = this.ratingRank(p.max_rating);
                if (have > allowed) {
                    const word = this.RATING_WORD[have];
                    const sentence = (p.rating_refusals && p.rating_refusals[word])
                        || `${label} doesn't take ${word} work — this piece is rated ${word}; it takes work up to ${this.RATING_WORD[allowed]}.`;
                    return { ok: false, reason: sentence };
                }
            }
            return { ok: true, reason: '' };
        },

        /* Measure a File: {media: {kind, duration_s, width, height, bytes}, poster: File|null, posterUrl}. */
        async measure(file, title) {
            const kind = this.kindOf(file.name);
            const media = { kind, bytes: file.size };
            if (kind === 'image' || !kind) return { media, poster: null, posterUrl: '' };
            try {
                if (kind === 'video') return await this._video(file, media);
                return await this._audio(file, media, title || file.name);
            } catch (e) {
                return { media, poster: null, posterUrl: '', error: e && e.message ? e.message : String(e) };
            }
        },

        _video(file, media) {
            return new Promise((resolve) => {
                const url = URL.createObjectURL(file);
                const v = document.createElement('video');
                v.muted = true; v.playsInline = true; v.preload = 'auto';
                let done = false;
                const finish = (poster, posterUrl) => {
                    if (done) return; done = true;
                    clearTimeout(timer);
                    try { URL.revokeObjectURL(url); } catch (e) { /* noop */ }
                    resolve({ media, poster, posterUrl });
                };
                const timer = setTimeout(() => finish(null, ''), 20000);
                let probing = false;                       // seeking past the end to learn a missing duration
                const seekForFrame = () => {
                    if (isFinite(v.duration) && v.duration > 0) media.duration_s = Math.round(v.duration * 1000) / 1000;
                    media.width = v.videoWidth || 0; media.height = v.videoHeight || 0;
                    const t = Math.min(1, (isFinite(v.duration) && v.duration > 0 ? v.duration : 2) / 2);
                    try { v.currentTime = t; } catch (e) { finish(null, ''); }
                };
                v.addEventListener('loadedmetadata', () => {
                    // A WebM recorded by MediaRecorder carries no duration in its header
                    // (duration reads Infinity); seeking far past the end makes the browser
                    // scan the file and report the real length on the next durationchange.
                    if (!isFinite(v.duration)) { probing = true; try { v.currentTime = 1e101; } catch (e) { probing = false; } }
                    if (!probing) seekForFrame();
                });
                v.addEventListener('durationchange', () => {
                    if (probing && isFinite(v.duration)) { probing = false; seekForFrame(); }
                });
                v.addEventListener('seeked', () => {
                    if (probing) return;
                    const c = this._fitCanvas(v.videoWidth || 640, v.videoHeight || 360);
                    try {
                        c.getContext('2d').drawImage(v, 0, 0, c.width, c.height);
                        c.toBlob(b => {
                            if (!b) return finish(null, '');
                            const poster = new File([b], this._posterName(file.name, 'jpg'), { type: 'image/jpeg' });
                            finish(poster, URL.createObjectURL(b));
                        }, 'image/jpeg', 0.9);
                    } catch (e) { finish(null, ''); }
                });
                v.addEventListener('error', () => finish(null, ''));
                v.src = url;
            });
        },

        async _audio(file, media, title) {
            const buf = await file.arrayBuffer();
            const Ctx = window.AudioContext || window.webkitAudioContext;
            const ctx = new Ctx();
            let audio;
            try { audio = await ctx.decodeAudioData(buf.slice(0)); }
            finally { try { ctx.close(); } catch (e) { /* noop */ } }
            media.duration_s = Math.round(audio.duration * 1000) / 1000;
            const poster = await this.waveformPoster(audio, title);
            return { media, poster: poster ? new File([poster], this._posterName(file.name, 'png'), { type: 'image/png' }) : null,
                     posterUrl: poster ? URL.createObjectURL(poster) : '' };
        },

        /* Peak envelope over `buckets` columns → the values the poster draws. Pure (testable). */
        envelope(channel, buckets) {
            const n = channel.length, out = new Array(buckets).fill(0);
            if (!n) return out;
            const per = n / buckets;
            for (let b = 0; b < buckets; b++) {
                const a = Math.floor(b * per), z = Math.min(n, Math.floor((b + 1) * per) || a + 1);
                let peak = 0;
                for (let i = a; i < z; i++) { const v = Math.abs(channel[i]); if (v > peak) peak = v; }
                out[b] = peak;
            }
            const max = Math.max(...out) || 1;
            return out.map(v => v / max);
        },

        waveformPoster(audio, title) {
            const W = 1600, H = 900, buckets = 800;
            const ch = audio.getChannelData(0);
            const env = this.envelope(ch, buckets);
            const c = document.createElement('canvas'); c.width = W; c.height = H;
            const ctx = c.getContext('2d');
            ctx.fillStyle = '#fbf8f3'; ctx.fillRect(0, 0, W, H);
            ctx.fillStyle = '#171717';
            const mid = H * 0.56, amp = H * 0.30, bw = W / buckets;
            for (let i = 0; i < buckets; i++) {
                const h = Math.max(2, env[i] * amp);
                ctx.fillRect(i * bw, mid - h, Math.max(1, bw - 1), h * 2);
            }
            ctx.fillStyle = 'rgba(23,23,23,0.85)';
            ctx.font = `600 ${Math.round(H * 0.045)}px 'Helvetica Neue', Arial, sans-serif`;
            ctx.textBaseline = 'top';
            const t = String(title || '').replace(/\.[^.]+$/, '').slice(0, 60);
            if (t) ctx.fillText(t, W * 0.04, H * 0.06);
            ctx.font = `${Math.round(H * 0.032)}px 'Helvetica Neue', Arial, sans-serif`;
            ctx.fillText('♪ ' + this.fmtDuration(audio.duration), W * 0.04, H * 0.13);
            return new Promise(res => c.toBlob(res, 'image/png'));
        },

        _fitCanvas(w, h) {
            const scale = Math.min(1, this.POSTER_MAX / Math.max(w, h, 1));
            const c = document.createElement('canvas');
            c.width = Math.max(1, Math.round(w * scale)); c.height = Math.max(1, Math.round(h * scale));
            return c;
        },
        _posterName(name, ext) {
            return String(name || 'media').replace(/\.[^.]+$/, '').replace(/[^\w.-]+/g, '_').slice(0, 60) + '-poster.' + ext;
        },
        mediaUrl(name, file) {
            return `/api/artwork/media?name=${encodeURIComponent(name)}&file=${encodeURIComponent(file)}`;
        },
    };

    if (typeof window !== 'undefined') window.MediaKinds = MediaKinds;
    if (typeof module !== 'undefined' && module.exports) module.exports = MediaKinds;
})();
