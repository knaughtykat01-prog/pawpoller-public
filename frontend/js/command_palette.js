/* PawPoller command palette — Cmd+K / Ctrl+K to open.
 *
 * Quick-nav overlay modelled on the GitHub / VS Code / Linear style.
 * Pure frontend; no backend changes. Lists every navigable page plus
 * a handful of common actions (trigger poll, toggle theme). Fuzzy
 * search ranks by prefix > substring > subsequence.
 *
 * Scope notes:
 *   - Commands are a flat list — no categories. The fuzzy ranker
 *     keeps related entries near each other when typing partials.
 *   - Up/Down/Enter/Esc work as expected. Tab is reserved for the
 *     browser's focus order and isn't intercepted.
 *   - Mouse hover sets the active row so keyboard and pointer
 *     navigation don't fight each other.
 */

(function () {
    // Canonical 11-platform list now lives in platforms.js (window.PLATFORMS),
    // loaded before this script. Map to the {code,label} shape the palette uses.
    const PLATFORMS = (window.PLATFORMS || []).map(p => ({ code: p.code, label: p.label }));

    const COMMANDS = [
        // Top-level pages
        { label: 'Overview',           hint: 'Cross-platform dashboard', href: '#/' },
        { label: 'Settings',           hint: 'All preferences',           href: '#/settings' },
        { label: 'Story Editor',       hint: 'Browse and edit stories',   href: '#/editor' },
        // Create actions (IA 2.142.0)
        { label: 'New Story',          hint: 'Create — write a story',    href: '#/editor', keywords: 'create write new' },
        { label: 'New Artwork',        hint: 'Create — upload an image',  href: '#/artwork/new', keywords: 'create upload image art' },
        { label: 'New Post',           hint: 'Create — compose a post',   href: '#/posts/new', keywords: 'create compose microblog tweet' },
        { label: 'Promo Maker',        hint: 'Create — excerpt image',    href: '#/promo', keywords: 'create marketing booktok' },
        { label: 'Posts',              hint: 'Your published posts',      href: '#/posts', keywords: 'microblog feed catalogue' },
        { label: 'Stories',            hint: 'Library — your stories',    href: '#/library/type/story', keywords: 'hub works' },
        { label: 'Artwork',            hint: 'Library — your artwork',    href: '#/library/type/artwork', keywords: 'hub works images' },
        { label: 'Discovered',         hint: 'Library — polled, unlinked', href: '#/library/type/discovered', keywords: 'review import ignore' },
        { label: 'Posting Queue',      hint: 'Pending uploads',           href: '#/posting/queue' },
        { label: 'Posting History',    hint: 'Audit trail',               href: '#/posting/log' },
        { label: 'Groups',             hint: 'Submission groups',         href: '#/groups' },
        { label: 'Collections',        hint: 'One piece across platforms', href: '#/collections' },
        { label: 'Analytics',          hint: 'Cross-platform analytics',  href: '#/analytics' },
        // Per-platform dashboards
        ...PLATFORMS.flatMap(p => [
            { label: `${p.label} dashboard`,    hint: `Open ${p.label} dashboard`, href: `#/${p.code}` },
            { label: `${p.label} submissions`,  hint: `Browse ${p.label} list`,    href: `#/${p.code}/submissions` },
        ]),
        // Actions — small list, only the ones with a clear single-shot effect
        {
            label: 'Toggle theme',
            hint: 'Open Settings → Appearance',
            href: '#/settings',
            keywords: 'dark light colour color appearance',
        },
        {
            label: 'Pause polling',
            hint: 'Stop all polls until resumed',
            keywords: 'stop pause polling',
            action: async () => {
                await fetch('/api/poll/pause', { method: 'POST' });
                if (window.toast) window.toast.warn('Polling paused');
            },
        },
        {
            label: 'Resume polling',
            hint: 'Resume polls (after pause)',
            keywords: 'unpause continue resume polling',
            action: async () => {
                await fetch('/api/poll/resume', { method: 'POST' });
                if (window.toast) window.toast.success('Polling resumed');
            },
        },
    ];

    function score(query, cmd) {
        if (!query) return 1; // empty query → list all in original order
        const q = query.toLowerCase().trim();
        const haystack = [cmd.label, cmd.hint || '', cmd.keywords || '']
            .join(' ').toLowerCase();
        if (cmd.label.toLowerCase().startsWith(q)) return 100;
        if (haystack.startsWith(q)) return 80;
        if (haystack.includes(q)) return 50;
        // Subsequence: every char in q appears in haystack in order
        let i = 0;
        for (const ch of haystack) {
            if (ch === q[i]) i++;
            if (i === q.length) return 25;
        }
        return 0;
    }

    function rank(query) {
        return COMMANDS
            .map(cmd => ({ cmd, s: score(query, cmd) }))
            .filter(x => x.s > 0)
            .sort((a, b) => b.s - a.s || a.cmd.label.localeCompare(b.cmd.label))
            .slice(0, 12)
            .map(x => x.cmd);
    }

    let overlayEl = null;
    let inputEl = null;
    let listEl = null;
    let activeIdx = 0;
    let currentResults = [];

    function ensureUI() {
        if (overlayEl) return;
        overlayEl = document.createElement('div');
        overlayEl.className = 'cmdk-overlay';
        overlayEl.setAttribute('hidden', '');
        overlayEl.innerHTML = `
            <div class="cmdk-panel" role="dialog" aria-label="Command palette">
                <input class="cmdk-input" type="text" placeholder="Jump to a page or run a command…" autocomplete="off" spellcheck="false">
                <ul class="cmdk-list" role="listbox"></ul>
                <div class="cmdk-footer">
                    <span><kbd>↑</kbd><kbd>↓</kbd> navigate</span>
                    <span><kbd>↵</kbd> select</span>
                    <span><kbd>Esc</kbd> close</span>
                </div>
            </div>
        `;
        document.body.appendChild(overlayEl);
        inputEl = overlayEl.querySelector('.cmdk-input');
        listEl = overlayEl.querySelector('.cmdk-list');

        inputEl.addEventListener('input', () => render(inputEl.value));
        inputEl.addEventListener('keydown', onKeyDown);
        overlayEl.addEventListener('click', (e) => {
            if (e.target === overlayEl) close();
        });
    }

    function render(query) {
        currentResults = rank(query);
        activeIdx = 0;
        if (currentResults.length === 0) {
            listEl.innerHTML = '<li class="cmdk-empty">No matches</li>';
            return;
        }
        listEl.innerHTML = currentResults.map((cmd, i) => `
            <li class="cmdk-row${i === activeIdx ? ' is-active' : ''}" data-idx="${i}" role="option">
                <span class="cmdk-row-label">${escapeHtml(cmd.label)}</span>
                ${cmd.hint ? `<span class="cmdk-row-hint">${escapeHtml(cmd.hint)}</span>` : ''}
            </li>
        `).join('');
        listEl.querySelectorAll('.cmdk-row').forEach(el => {
            el.addEventListener('mouseenter', () => setActive(parseInt(el.dataset.idx, 10)));
            el.addEventListener('click', () => execute(currentResults[parseInt(el.dataset.idx, 10)]));
        });
    }

    function setActive(idx) {
        if (idx < 0 || idx >= currentResults.length) return;
        activeIdx = idx;
        listEl.querySelectorAll('.cmdk-row').forEach((el, i) => {
            el.classList.toggle('is-active', i === idx);
        });
        const activeRow = listEl.children[idx];
        if (activeRow && activeRow.scrollIntoView) {
            activeRow.scrollIntoView({ block: 'nearest' });
        }
    }

    function onKeyDown(e) {
        if (e.key === 'ArrowDown') {
            e.preventDefault();
            setActive(Math.min(activeIdx + 1, currentResults.length - 1));
        } else if (e.key === 'ArrowUp') {
            e.preventDefault();
            setActive(Math.max(activeIdx - 1, 0));
        } else if (e.key === 'Enter') {
            e.preventDefault();
            if (currentResults[activeIdx]) execute(currentResults[activeIdx]);
        } else if (e.key === 'Escape') {
            e.preventDefault();
            close();
        }
    }

    function execute(cmd) {
        if (!cmd) return;
        close();
        try {
            if (cmd.action) {
                Promise.resolve(cmd.action()).catch(err => {
                    console.error('[cmdk] action failed', err);
                    if (window.toast) window.toast.error(`Command failed: ${err.message || err}`);
                });
            } else if (cmd.href) {
                window.location.hash = cmd.href;
            }
        } catch (e) {
            console.error('[cmdk] execute failed', e);
        }
    }

    function open() {
        ensureUI();
        overlayEl.removeAttribute('hidden');
        inputEl.value = '';
        render('');
        // Defer focus so the keystroke that opened the palette
        // doesn't immediately register in the input.
        requestAnimationFrame(() => inputEl.focus());
    }

    function close() {
        if (!overlayEl) return;
        overlayEl.setAttribute('hidden', '');
    }

    function escapeHtml(s) {
        return String(s).replace(/[&<>"']/g, (c) => ({
            '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
        }[c]));
    }

    document.addEventListener('keydown', (e) => {
        // Cmd+K (mac) or Ctrl+K (linux/win). Ignore inside contenteditable
        // / input fields where Ctrl+K might be a real shortcut (e.g. the
        // CodeMirror editor's own bindings).
        const isShortcut = (e.metaKey || e.ctrlKey) && (e.key === 'k' || e.key === 'K');
        if (!isShortcut) return;
        const target = e.target;
        const tag = (target && target.tagName) || '';
        if (tag === 'INPUT' || tag === 'TEXTAREA' || (target && target.isContentEditable)) {
            // Allow Cmd+K to close the palette even when an input is
            // focused (so the palette's own input doesn't trap it).
            if (overlayEl && !overlayEl.hasAttribute('hidden')) {
                e.preventDefault();
                close();
                return;
            }
            return;
        }
        e.preventDefault();
        if (overlayEl && !overlayEl.hasAttribute('hidden')) close();
        else open();
    });

    window.CommandPalette = { open, close };
})();
