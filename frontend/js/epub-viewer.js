// In-app EPUB viewer logic. Extracted to its own file so the page's
// inline-script CSP hash doesn't have to maintain a second entry —
// 'self' covers /js/ and /vendor/, so external files load freely.
(function () {
    const params = new URLSearchParams(window.location.search);
    const story = params.get('story');
    const file  = params.get('file');
    const status = document.getElementById('viewer-status');
    const titleEl = document.getElementById('viewer-title');
    const locEl   = document.getElementById('viewer-location');
    const dl      = document.getElementById('btn-download');

    function fail(msg) {
        status.textContent = msg;
        status.classList.add('error');
    }

    if (!story || !file) {
        fail('Missing ?story or ?file in URL.');
        return;
    }

    const epubUrl = `/api/posting/file?story=${encodeURIComponent(story)}&file=${encodeURIComponent(file)}`;
    dl.href = epubUrl;
    titleEl.textContent = file.split('/').pop().replace(/\.epub$/i, '');

    // ── Reader preferences (persisted) ─────────────────────────
    // Key by story+file so different books can have independent
    // size/theme/location. Wrapped in a try/catch so storage-quota
    // exceeded or disabled-cookies modes degrade gracefully.
    const PREF_KEY = `pawpoller-epub:${story}:${file}`;
    function loadPrefs() {
        try { return JSON.parse(localStorage.getItem(PREF_KEY) || '{}'); }
        catch (e) { return {}; }
    }
    function savePrefs(patch) {
        try {
            const cur = loadPrefs();
            localStorage.setItem(PREF_KEY, JSON.stringify({ ...cur, ...patch }));
        } catch (e) { /* ignore */ }
    }
    const prefs = loadPrefs();
    const FONT_SIZES  = { S: '90%', M: '105%', L: '125%', XL: '150%' };
    const READER_THEMES = ['auto', 'light', 'dark', 'sepia'];
    let currentSize  = FONT_SIZES[prefs.size] ? prefs.size : 'M';
    let currentTheme = READER_THEMES.includes(prefs.theme) ? prefs.theme : 'auto';

    // ── Theme palettes ────────────────────────────────────────
    // 'auto' resolves to the parent dashboard's theme tokens at load
    // time. The other three are book-style palettes hard-coded so the
    // reader stays usable even if the parent theme is something garish.
    function paletteForTheme(name) {
        if (name === 'light')  return { bg: '#fafaf6', text: '#1a1a1a', accent: '#7e5cd6' };
        if (name === 'dark')   return { bg: '#13111a', text: '#f0edf5', accent: '#9b7dff' };
        if (name === 'sepia')  return { bg: '#f4ecd8', text: '#3a2e21', accent: '#7a4f1e' };
        const cs = getComputedStyle(document.documentElement);
        return {
            bg:     cs.getPropertyValue('--bg-primary').trim()    || '#fafaf6',
            text:   cs.getPropertyValue('--text-primary').trim()  || '#1a1a1a',
            accent: cs.getPropertyValue('--accent').trim()        || '#9b7dff',
        };
    }

    // openAs: 'epub' forces zip-archive parsing. Without it, epub.js
    // sniffs the URL's file extension to pick archive vs. directory
    // mode — and our URL path is `/api/posting/file` (the .epub lives
    // in the query string), so the sniff fails and the load hangs
    // trying to read META-INF/container.xml as a directory. Same-
    // origin fetch carries the pp_session cookie automatically.
    const book = ePub(epubUrl, { openAs: 'epub' });
    const rendition = book.renderTo('viewer', {
        width:  '100%',
        height: '100%',
        flow:   'paginated',
        spread: 'auto',
    });

    function applyTheme() {
        const p = paletteForTheme(currentTheme);
        // Apply to the page chrome so the toolbar/background match.
        document.documentElement.style.setProperty('--reader-bg',     p.bg);
        document.documentElement.style.setProperty('--reader-text',   p.text);
        document.documentElement.style.setProperty('--reader-accent', p.accent);
        // Push concrete values into the rendered iframe — CSS custom
        // properties don't cross the iframe boundary, so we have to
        // pass colours rather than var(--…) references. Cover gets
        // promoted to full-bleed via height/object-fit hints; some
        // readers wrap the cover image in a small <figure> by default.
        rendition.themes.default({
            'body': {
                'background': p.bg,
                'color':      p.text,
                'font-family': "'Crimson Pro', Georgia, 'Times New Roman', serif",
                'line-height': '1.55',
                'padding':    '0 1em',
            },
            'p': { 'margin': '0 0 0.8em 0' },
            'a': { 'color': p.accent },
            'h1, h2, h3, h4': { 'color': p.text },
            // Full-page cover — most EPUB cover.xhtml templates use
            // either a bare <img> or wrap one in a <figure>/<svg>.
            // Cap by viewport height with object-fit so the image
            // fills the page rather than rendering at intrinsic size.
            'img': {
                'max-width':  '100%',
                'max-height': '95vh',
                'height':     'auto',
                'display':    'block',
                'margin':     '0 auto',
            },
            'figure': { 'margin': '0', 'text-align': 'center' },
            'svg':    { 'max-width': '100%', 'max-height': '95vh' },
        });
        rendition.themes.fontSize(FONT_SIZES[currentSize]);
    }

    applyTheme();

    // ── Render + restore last position ────────────────────────
    // book.ready resolves once the spine is parsed; we display the
    // saved CFI if there is one, otherwise the first page.
    book.ready.then(() => {
        const target = prefs.cfi || undefined;
        return rendition.display(target);
    }).then(() => {
        status.style.display = 'none';
    }).catch((err) => {
        fail('Failed to open EPUB: ' + (err && err.message ? err.message : err));
    });

    book.loaded.metadata.then((meta) => {
        if (meta && meta.title) titleEl.textContent = meta.title;
    }).catch(() => { /* ignore */ });

    book.ready.then(() => book.locations.generate(1024)).then(() => {
        updateLocation();
    }).catch(() => { /* locations are optional */ });

    function updateLocation() {
        try {
            const loc = rendition.currentLocation();
            if (!loc || !loc.start) return;
            const cfi = loc.start.cfi;
            // Persist current CFI so the next open lands here.
            savePrefs({ cfi });
            const pct = book.locations && book.locations.length()
                ? Math.round(book.locations.percentageFromCfi(cfi) * 100)
                : null;
            locEl.textContent = pct !== null ? pct + '%' : '';
        } catch (e) { /* ignore */ }
    }

    rendition.on('relocated', updateLocation);

    // ── Navigation ────────────────────────────────────────────
    const prev = () => rendition.prev();
    const next = () => rendition.next();

    document.getElementById('btn-prev').addEventListener('click', prev);
    document.getElementById('btn-next').addEventListener('click', next);
    document.getElementById('tap-prev').addEventListener('click', prev);
    document.getElementById('tap-next').addEventListener('click', next);
    document.getElementById('btn-close').addEventListener('click', () => window.close());

    document.addEventListener('keydown', (e) => {
        if (e.key === 'ArrowLeft')  prev();
        if (e.key === 'ArrowRight') next();
    });

    rendition.on('keyup', (e) => {
        if (e.key === 'ArrowLeft')  prev();
        if (e.key === 'ArrowRight') next();
    });

    // ── Aa appearance dropdown ────────────────────────────────
    const aaBtn  = document.getElementById('btn-aa');
    const aaMenu = document.getElementById('aa-menu');

    function refreshAaMenuState() {
        // Reflect current selection on the buttons.
        aaMenu.querySelectorAll('[data-size]').forEach(b => {
            b.classList.toggle('active', b.dataset.size === currentSize);
        });
        aaMenu.querySelectorAll('[data-theme-name]').forEach(b => {
            b.classList.toggle('active', b.dataset.themeName === currentTheme);
        });
    }

    aaBtn.addEventListener('click', (e) => {
        e.stopPropagation();
        aaMenu.classList.toggle('open');
        refreshAaMenuState();
    });
    document.addEventListener('click', (e) => {
        if (!aaMenu.contains(e.target) && e.target !== aaBtn) {
            aaMenu.classList.remove('open');
        }
    });

    aaMenu.addEventListener('click', (e) => {
        const sizeBtn  = e.target.closest('[data-size]');
        const themeBtn = e.target.closest('[data-theme-name]');
        if (sizeBtn) {
            currentSize = sizeBtn.dataset.size;
            savePrefs({ size: currentSize });
            rendition.themes.fontSize(FONT_SIZES[currentSize]);
            refreshAaMenuState();
        } else if (themeBtn) {
            currentTheme = themeBtn.dataset.themeName;
            savePrefs({ theme: currentTheme });
            applyTheme();
            refreshAaMenuState();
        }
    });
})();
