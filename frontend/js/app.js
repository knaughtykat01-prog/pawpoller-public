/* ── SPA Router & Page Rendering ───────────────────────────── */
/*
 * Main application singleton — hash-based SPA router, page renderers,
 * and state management. Coordinates API calls, component rendering,
 * and chart creation for all pages (Inkbunny, FurAffinity, Weasyl,
 * cross-platform overview, groups, settings).
 */

const App = {
    /* ── Session-Persisted State ──────────────────────────────
     * These properties survive re-renders within the same browser
     * session but reset to their defaults on a fresh page load.
     *
     * _sortState / _faSortState / _wsSortState:
     *   Current column sort for the IB / FA / WS submissions tables.
     *   { field: 'views'|'favorites_count'|..., order: 'asc'|'desc' }
     *
     * _dateRange:
     *   Active date-range filter ('all', '7d', '30d', '90d', 'year')
     *   shared by dashboard and detail chart date-range bars.
     *
     * _compareIds / _faCompareIds / _wsCompareIds:
     *   Set of submission IDs currently selected on the IB / FA / WS
     *   comparison page (max 5 each).
     *
     * _compareMetric / _faCompareMetric / _wsCompareMetric:
     *   Metric shown on the comparison overlay chart ('views',
     *   'favorites_count', 'comments_count').
     *
     * _autoRefreshTimer:
     *   Handle returned by setInterval for the 60-second auto-refresh
     *   cycle. Cleared on every route change so stale timers don't fire.
     *
     * _autoRefreshInterval:
     *   Interval in milliseconds (60 000 = 60 s). The auto-refresh
     *   re-renders the current page to show fresh poll data. It is
     *   paused automatically when the browser tab is hidden
     *   (document.hidden check inside _startAutoRefresh).
     */
    currentPage: null,
    _sortState: { field: 'views', order: 'desc' },
    _dateRange: 'all',
    _compareIds: new Set(),
    _compareMetric: 'views',
    _autoRefreshTimer: null,
    _autoRefreshInterval: 60000,
    _faSortState: { field: 'views', order: 'desc' },
    _faCompareIds: new Set(),
    _faCompareMetric: 'views',
    _wsSortState: { field: 'views', order: 'desc' },
    _wsCompareIds: new Set(),
    _wsCompareMetric: 'views',
    _sfSortState: { field: 'views', order: 'desc' },
    _sfCompareIds: new Set(),
    _sfCompareMetric: 'views',
    _sqwSortState: { field: 'views', order: 'desc' },
    _sqwCompareIds: new Set(),
    _sqwCompareMetric: 'views',
    _ao3SortState: { field: 'views', order: 'desc' },
    _ao3CompareIds: new Set(),
    _ao3CompareMetric: 'views',
    _daSortState: { field: 'views', order: 'desc' },
    _daCompareIds: new Set(),
    _daCompareMetric: 'views',
    _wpSortState: { field: 'reads', order: 'desc' },
    _wpCompareIds: new Set(),
    _wpCompareMetric: 'reads',
    _ikSortState: { field: 'likes', order: 'desc' },
    _ikCompareIds: new Set(),
    _ikCompareMetric: 'likes',
    _bskySortState: { field: 'likes', order: 'desc' },
    _bskyCompareIds: new Set(),
    _bskyCompareMetric: 'likes',
    _mastSortState: { field: 'likes', order: 'desc' },
    _mastCompareIds: new Set(),
    _mastCompareMetric: 'likes',
    _tumSortState: { field: 'notes', order: 'desc' },
    _tumCompareIds: new Set(),
    _pixSortState: { field: 'views', order: 'desc' },
    _pixCompareIds: new Set(),
    _pixCompareMetric: 'views',
    _thrSortState: { field: 'views', order: 'desc' },
    _thrCompareIds: new Set(),
    _thrCompareMetric: 'views',
    _igSortState: { field: 'views', order: 'desc' },
    _igCompareIds: new Set(),
    _igCompareMetric: 'views',
    _e621SortState: { field: 'score', order: 'desc' },
    _e621CompareIds: new Set(),
    _e621CompareMetric: 'score',
    _fnSortState: { field: 'views', order: 'desc' },
    _fnCompareIds: new Set(),
    _fnCompareMetric: 'views',
    _fbrSortState: { field: 'score', order: 'desc' },
    _fbrCompareIds: new Set(),
    _fbrCompareMetric: 'score',
    _twSortState: { field: 'views', order: 'desc' },
    _twCompareIds: new Set(),
    _twCompareMetric: 'views',

    /*
     * init() — Boot sequence, called once from index.html on DOMContentLoaded.
     *
     * 1. Registers the hashchange listener so every URL fragment change
     *    triggers route().
     * 2. Auth check: redirects to #/login if the server has no stored
     *    credentials, or to #/loading if credentials exist but no
     *    submission data has been fetched yet.
     * 3. Fires the initial route() to render whatever hash is in the URL.
     * 4. Starts a 60-second poll-status interval that updates the
     *    "last polled" badge in the sidebar.
     * 5. Creates the hamburger menu toggle + sidebar overlay for mobile
     *    viewports (< 768 px). Clicking the overlay or any nav link
     *    closes the mobile sidebar.
     * 6. Wires up the logout button to call API.authLogout then redirect
     *    to the login screen.
     */
    async init() {
        /* Listen for hash changes so browser back/forward works */
        window.addEventListener('hashchange', () => this.route());

        /* ── CSP-safe click delegation ──────────────────────────────
         * Our Content-Security-Policy (dashboard.py) is strict:
         * `script-src 'self' <hash>` with NO 'unsafe-inline', so the
         * browser BLOCKS every inline on*="…" handler ("Executing inline
         * event handler violates … Content Security Policy"). All click
         * actions therefore route through this ONE document-level listener,
         * keyed on data-* attributes set on the button/card. Attached here,
         * before the auth-gate early-returns below, so it also works on the
         * login/setup screens. Handlers that used to be inline onclick=""
         * (navigate, poll/resync, export, link/group/posting actions) now
         * carry a data-attribute instead. (2.51.4) */
        document.addEventListener('click', (e) => {
            const el = e.target.closest(
                '[data-nav],[data-poll],[data-resync],[data-export],' +
                '[data-pause-plat],[data-resume-plat],' +
                '[data-link-stats],[data-link-delete],[data-remove-group],[data-post-action],' +
                '[data-link-suggest]'
            );
            if (!el) return;
            const d = el.dataset;
            if (d.nav !== undefined) { this.navigate(d.nav); return; }
            if (d.poll !== undefined) { this._dashPoll(el, d.poll); return; }
            if (d.resync !== undefined) { this._dashResync(el, d.resync); return; }
            if (d.pausePlat !== undefined) { this._togglePlatformPause(el, d.pausePlat, true); return; }
            if (d.resumePlat !== undefined) { this._togglePlatformPause(el, d.resumePlat, false); return; }
            if (d.export !== undefined) { API.exportSubmissions(d.export); return; }
            if (d.linkStats !== undefined) { this.viewLinkStats(Number(d.linkStats)); return; }
            if (d.linkDelete !== undefined) { this.deleteLink(Number(d.linkDelete)); return; }
            if (d.linkSuggest !== undefined) {  // CSP-safe replacement for the old inline onclick
                try { this.createLinkFromSuggestion(JSON.parse(el.dataset.items || '[]')); }
                catch (err) { /* malformed items — ignore */ }
                return;
            }
            if (d.removeGroup !== undefined) {
                this.removeGroupMember(Number(d.removeGroup), d.removePlatform, d.removeSub);
                return;
            }
            if (d.postAction !== undefined && typeof Posting !== 'undefined') {
                switch (d.postAction) {
                    case 'update-single': Posting._updateSingle(d.postStory, d.postPlatform, Number(d.postChapter)); break;
                    case 'upload-to':     Posting._uploadTo(d.postStory, d.postPlatform); break;
                    case 'update-all':    Posting._updateAll(d.postStory); break;
                    case 'cancel-queue':  Posting._cancelQueue(Number(d.postQueue)); break;
                }
                return;
            }
        });

        /* Image load-error fallback — CSP-safe replacement for inline
         * onerror="". The `error` event doesn't bubble, so listen in the
         * capture phase; any <img data-img-fallback> that fails to load
         * hides itself and reveals its next sibling (the placeholder). (2.51.4) */
        document.addEventListener('error', (e) => {
            const t = e.target;
            if (t && t.matches && t.matches('img[data-img-fallback]')) {
                t.style.display = 'none';
                const sib = t.nextElementSibling;
                if (sib) sib.style.display = 'flex';
            }
        }, true);

        /* Sidebar expansion → reflow main content. Bind listeners EARLY,
         * before the dashboard/setup auth gates that can return out of
         * init() — the sidebar element lives in the static index.html so
         * it's always present, and we don't want the listeners skipped
         * when the user is briefly on the login or setup screen first.
         * (BUG-008 in 2.14.6, regression caught in 2.14.7 QA.) */
        /* Sidebar collapse/pin — explicit toggle (replaces the old
         * hover-to-expand rail). State persists in localStorage and is
         * applied as `.collapsed` on the sidebar + `sidebar-collapsed` on
         * <body> (the latter drives the main column's left margin). */
        const _sidebarEl = document.querySelector('.sidebar');
        const _applyCollapsed = (on) => {
            _sidebarEl?.classList.toggle('collapsed', on);
            document.body.classList.toggle('sidebar-collapsed', on);
        };
        try { _applyCollapsed(localStorage.getItem('pawpoller-sidebar-collapsed') === '1'); } catch (e) { /* ignore */ }
        document.getElementById('sidebar-collapse')?.addEventListener('click', () => {
            const on = !_sidebarEl?.classList.contains('collapsed');
            _applyCollapsed(on);
            try { localStorage.setItem('pawpoller-sidebar-collapsed', on ? '1' : '0'); } catch (e) { /* ignore */ }
        });

        /* Dashboard auth gate — check if dashboard login is required BEFORE
         * the Inkbunny auth check.  This is the outer auth layer. */
        try {
            const dashStatus = await API.getDashboardStatus();
            this._dashboardAuthRequired = dashStatus.auth_required;
            this._dashboardStatus = dashStatus;
            if (dashStatus.auth_required && !dashStatus.authenticated) {
                window.location.hash = '#/dashboard-login';
                this.route();
                return;  // Don't proceed with Inkbunny auth or status ticker
            }
            // Authenticated — redirect away from login/setup screens
            const h = window.location.hash.replace('#/', '');
            if (h === 'dashboard-login' || h === 'dashboard-setup') {
                window.location.hash = '#/';
            }
        } catch (err) {
            console.warn('[App] Dashboard status check failed:', err);
        }

        // Past the auth gate — safe to start the platform health
        // poller. /api/platforms/health requires a valid session, so
        // starting earlier would 401-spam the login page on its 60s tick.
        if (window.PlatformHealth) {
            window.PlatformHealth.start();
        }
        // Notification centre (bell + feed) — same auth gate: /api/notifications
        // needs a session. Starts after PlatformHealth so it can reuse its
        // LABELS + relative-time helpers.
        if (window.NotificationCenter) {
            window.NotificationCenter.start();
        }
        // App-wide achievement watcher — pops the Laurels celebration the moment
        // a poll crosses a milestone, on whatever screen you're on (not just the
        // Laurels page). Subscribes to PlatformHealth to detect poll completion;
        // shares its seen-baseline with the page, so each crossing fires once.
        if (window.Laurels && window.Laurels.startAchievementWatch) {
            window.Laurels.startAchievementWatch();
        }
        // Warm the guided-tour seen-set from the server now that we're past the
        // auth gate. /api/settings/preferences needs a session, and hydrating
        // here (before the first route() below) means a tour the user already
        // dismissed on another browser/the PWA won't flash before the set loads.
        if (window.Tour && window.Tour.hydrate) {
            window.Tour.hydrate();
        }

        /* First-run setup wizard — if setup_complete is not set, show
         * the guided wizard instead of the normal dashboard. This check
         * runs after dashboard auth (so the user is authenticated) but
         * before any platform auth gates. */
        try {
            const setupResp = await API.getSetupStatus();
            if (!setupResp.setup_complete) {
                window.location.hash = '#/setup';
                this.route();
                return;  // Don't proceed with platform auth — wizard handles it
            }
        } catch (err) {
            console.warn('[App] Setup status check failed:', err);
        }

        /* Inkbunny platform auth gate — decide which screen the user should
         * land on. Two important rules learned in 2.14.7 (BUG-005/006/007):
         *
         * 1. Never force-redirect to the legacy IB-only login screen on
         *    landing. Server installs and AO3/FA-only users may not have
         *    IB credentials at all, but they still need the dashboard.
         *    They configure IB (if they want it) via Settings → Platforms.
         *
         * 2. Only auto-route to the loading screen on root nav. Deep links
         *    like #/settings/general should resolve straight there — the
         *    user is fixing their config, not waiting for a poll. */
        try {
            const auth = await API.getAuthStatus();
            const currentHash = (window.location.hash || '').replace(/^#\/?/, '');
            const goingToRoot = !currentHash || currentHash === '/' || currentHash === '';
            if (auth.has_credentials && !auth.has_data && goingToRoot) {
                window.location.hash = '#/loading';
            }
        } catch (err) {
            console.warn('[App] Auth status check failed:', err);
        }

        /* Render the initial page and kick off the status check ticker */
        this.route();
        this._updateStatusCheck();
        if (this._statusCheckInterval) clearInterval(this._statusCheckInterval);
        this._statusCheckInterval = setInterval(() => this._updateStatusCheck(), 60000);
        this._initProgressCheckBar();

        /* Guided tours auto-fire from route() (see the Tour.maybeAuto hook
           there): getting-started once on the overview first-run, then each
           page's own tour once on first visit after getting-started is done. */

        /* Hamburger menu — overlay is now in HTML, just query it.
         * (Sidebar hover/focus listeners are bound early in init() above
         * so they survive the auth-gate early-returns.) */
        const sidebar = document.querySelector('.sidebar');
        const overlay = document.getElementById('sidebar-overlay');

        const closeSidebar = () => {
            sidebar?.classList.remove('open');
            overlay?.classList.remove('open');
            document.body.classList.remove('sidebar-open');
        };
        const openSidebar = () => {
            sidebar?.classList.add('open');
            overlay?.classList.add('open');
            /* Tracks mobile sidebar state so the (out-of-tree)
             * hamburger button can shift left when the panel
             * slides open. See BUG-010 in CHANGELOG. */
            document.body.classList.add('sidebar-open');
        };

        /* Toggle sidebar open/closed when the hamburger icon is tapped */
        document.getElementById('hamburger-btn')?.addEventListener('click', () => {
            if (sidebar?.classList.contains('open')) closeSidebar();
            else openSidebar();
        });

        /* Tapping the translucent overlay closes the sidebar */
        overlay?.addEventListener('click', closeSidebar);

        /* Close sidebar on nav click (mobile) so the page behind is visible */
        document.querySelectorAll('.nav-link').forEach(link => {
            link.addEventListener('click', closeSidebar);
        });

        /* Surfaced search → command palette (sidebar box, plus ⌘/Ctrl+K
         * which command_palette.js binds globally). */
        document.getElementById('sidebar-search')?.addEventListener('click', () => {
            window.CommandPalette?.open();
        });

        /* Mobile bottom-nav "More" opens the full nav drawer (the same
         * sidebar, slid in). Platforms now has its own hub page (#/platforms)
         * so the old popover is gone. */
        document.getElementById('bottom-nav-more')?.addEventListener('click', openSidebar);

        /* Chart expand modal — click any chart to view full-size */
        Charts.bindExpandHandlers();

        /* Nav-group dropdowns (top-bar mode) / accordion (mobile drawer).
           One delegated document handler so a dropdown can never get stuck
           open (2.72.1 — the first cut only toggled .expanded on the label and
           nothing cleared it, so click-outside left it pinned open):
             - click a group label → toggle just that group, close siblings
             - click anything else (a dropdown item, page content, outside) → close all
             - Escape → close all
           On close we also blur() the label so CSS :focus-within can't keep the
           panel open after .expanded is removed. In side-rail mode the .nav-sub
           lists are always shown via CSS, so toggling .expanded there is a
           harmless no-op. */
        const _closeNavGroups = (except) => {
            let changed = false;
            document.querySelectorAll('.nav-group.expanded').forEach(g => {
                if (g === except) return;
                g.classList.remove('expanded');
                g.querySelector('[data-nav-toggle]')?.setAttribute('aria-expanded', 'false');
                changed = true;
            });
            const active = document.activeElement;
            if (active && active.classList && active.classList.contains('nav-group-label')
                && (!except || active.closest('.nav-group') !== except)) {
                active.blur();
            }
            return changed;
        };
        document.addEventListener('click', (e) => {
            const toggle = e.target.closest('.nav-group-label[data-nav-toggle]');
            if (toggle) {
                const group = toggle.closest('.nav-group');
                const willOpen = group && !group.classList.contains('expanded');
                _closeNavGroups(group);                 // collapse any sibling group
                if (group) {
                    if (willOpen) {
                        group.classList.add('expanded');
                        toggle.setAttribute('aria-expanded', 'true');
                    } else {
                        group.classList.remove('expanded');
                        toggle.setAttribute('aria-expanded', 'false');
                        toggle.blur();                  // let :focus-within release it
                    }
                }
                return;
            }
            /* Any other click — a dropdown item, page content, outside — closes
               everything that's pinned open. */
            _closeNavGroups(null);
        });
        document.addEventListener('keydown', (e) => {
            if (e.key === 'Escape') _closeNavGroups(null);
        });

        /* 2.16.10 introduced a master collapse for the 11 platform
           sub-groups. 2.16.12 removed the wrapper entirely — the
           existing Platforms popover trigger covers the same ground
           more visually. Handler kept as a no-op stub in case anyone
           re-introduces the wrapper in the future. */

        /* Logout button — clears dashboard session if dashboard auth is active,
         * otherwise clears Inkbunny platform session */
        document.getElementById('logout-btn')?.addEventListener('click', async () => {
            if (this._dashboardAuthRequired) {
                try { await API.dashboardLogout(); } catch { /* ignore */ }
                this.navigate('/dashboard-login');
            } else {
                try { await API.authLogout(); } catch { /* ignore */ }
                this.navigate('/login');
            }
        });

        /* Theme — initial application happens inline in index.html before
           CSS evaluates (so no flash on load). Sidebar button now opens
           the Settings → Appearance picker since 8 themes don't fit a
           binary toggle. */
        document.getElementById('theme-toggle-btn')?.addEventListener('click', () => {
            this.navigate('/settings/appearance');
        });

        /* Safe (SFW) mode — the 🔒 footer button blurs every adult (non-general)
           cover across the app. `data-sfw` is already applied pre-paint in
           index.html; here we just sync the button state on boot and flip it on
           click, persisting to localStorage. A per-device viewing preference,
           deliberately NOT a server setting — it's about who's looking at THIS
           screen right now, not an account-wide policy. */
        const _sfwBtn = document.getElementById('sfw-toggle-btn');
        const _syncSfwBtn = () => {
            if (!_sfwBtn) return;
            const on = document.documentElement.dataset.sfw === '1';
            _sfwBtn.setAttribute('aria-pressed', on ? 'true' : 'false');
            _sfwBtn.innerHTML = on ? '&#128737;&#65039;' : '&#128274;';  // 🛡️ on / 🔒 off
            _sfwBtn.title = on
                ? 'Safe mode: on — adult content is blurred. Click to show all (NSFW).'
                : 'Safe mode: off — adult content visible. Click to hide adult content (SFW).';
        };
        _syncSfwBtn();
        _sfwBtn?.addEventListener('click', () => {
            const on = document.documentElement.dataset.sfw !== '1';
            if (on) {
                document.documentElement.dataset.sfw = '1';
                // Re-enabling re-hides everything: clear any lingering peeks.
                document.querySelectorAll('.sfw-revealed')
                    .forEach(el => el.classList.remove('sfw-revealed'));
            } else {
                delete document.documentElement.dataset.sfw;
            }
            try { localStorage.setItem('pawpoller-sfw', on ? '1' : '0'); } catch (e) { /* ignore */ }
            _syncSfwBtn();
            window.toast?.info(on
                ? 'Safe mode on — adult content hidden (click any blurred item to peek)'
                : 'Safe mode off — showing all content');
        });

        /* Click-to-reveal "peek". One delegated capture-phase listener covers
           every blurred surface — current and future — with no per-card markup.
           In safe mode the first click on a still-blurred tile reveals just that
           one (and swallows the click so the card/link doesn't navigate); the
           next click on the now-revealed tile falls through to normal navigation.
           The selector mirrors safe_mode.css's blur set; `.mp-alts img` matches
           the alt thumbnails whose rating lives on the .mp-alts wrapper. Peeks
           reset naturally on SPA navigation (the grid re-renders). */
        const _sfwPeekSel = [
            '.book-cover:not([data-rating="general"])',
            '.artwork-card-cover:not([data-rating="general"])',
            '.mp-cover:not([data-rating="general"])',
            '.coll-cover:not([data-rating="general"])',
            '.mp-alts:not([data-rating="general"]) img',
            'img.artwork-detail-img:not([data-rating="general"])',
            'img.mp-hero-img:not([data-rating="general"])',
            'img.post-card-img:not([data-rating="general"])',
        ].join(', ');
        document.addEventListener('click', (e) => {
            if (document.documentElement.dataset.sfw !== '1') return;
            const el = e.target.closest?.(_sfwPeekSel);
            if (!el || el.classList.contains('sfw-revealed')) return;  // already peeked → let it through
            e.preventDefault();
            e.stopPropagation();
            el.classList.add('sfw-revealed');
        }, true);  // capture: intercept before the link/card navigation handlers

        /* Guided tours — the sidebar "?" runs the tour for wherever you are:
           the getting-started shell tour on the overview, or that page's own
           tour elsewhere (Tour.startHere resolves it from the current hash).
           Auto-firing (getting-started on first run, then each page once) is
           handled by Tour.maybeAuto(), scheduled from route(). */
        document.getElementById('help-tour-btn')?.addEventListener('click', () => {
            window.Tour?.startHere({ auto: false });
        });

        /* Sidebar version + update check */
        this._initSidebarVersion();

        /* Watch viewport so `mobile_mode='auto'` flips data-mobile when
           the user rotates the phone or resizes the window. No-op on
           forced on/off. */
        this._initMobileModeWatcher();

        /* Cross-device sync: when the tab regains focus (user comes back
           after editing settings on another device or in the desktop app),
           refresh the theme + general prefs so changes flow through without
           requiring a manual reload. Throttled to once per 3 seconds so
           rapid alt-tabs don't hammer the API. */
        document.addEventListener('visibilitychange', () => {
            if (document.hidden) return;
            const now = Date.now();
            if (this._lastFocusSync && now - this._lastFocusSync < 3000) return;
            this._lastFocusSync = now;
            this._refreshPrefsFromServer();
        });
    },

    /* Pull the latest preferences from the server and apply any drift.
       Currently only the theme has a visible effect — but pulling all
       prefs keeps client-side caches honest if we add more later. */
    async _refreshPrefsFromServer() {
        try {
            const prefs = await API.getPreferences();
            const serverTheme = prefs && prefs.theme;
            if (serverTheme && serverTheme !== this.getCurrentTheme()
                && this.THEMES.some(t => t.id === serverTheme)) {
                document.documentElement.dataset.theme = serverTheme;
                localStorage.setItem('pawpoller-theme', serverTheme);
                if (typeof Charts !== 'undefined' && Charts.destroyAll) Charts.destroyAll();
                if (this.route) this.route();
            }
            const serverMobile = prefs && prefs.mobile_mode;
            if (serverMobile && serverMobile !== this.getMobileModeOverride()
                && this.MOBILE_MODES.includes(serverMobile)) {
                // Apply without re-POSTing back (savePreferences would echo)
                localStorage.setItem('pawpoller-mobile-mode', serverMobile);
                const next = this._resolveMobile(serverMobile);
                if (document.documentElement.dataset.mobile !== next) {
                    document.documentElement.dataset.mobile = next;
                    if (typeof Charts !== 'undefined' && Charts.destroyAll) Charts.destroyAll();
                    if (this.route) this.route();
                }
            }
        } catch (e) { /* offline, ignore */ }
    },

    /* ── Theme catalog ───────────────────────────────────────
       Every theme defined here MUST also have a matching
       `[data-theme="<id>"]` block in tokens.css. The preview swatch
       colours are intentionally short (5 picks) — bg, card, accent,
       accent-warm, text — used by the picker card to render a live
       miniature without re-applying the full token block. */
    THEMES: [
        { id: 'quill',          name: 'Quill',          desc: 'Warm paper + sienna quill — the redesign',
          swatch: ['#f4f0e8', '#fffdf8', '#9a5b34', '#c07d3a', '#241d17'] },
        { id: 'quill_dark',     name: 'Quill Dark',     desc: 'Warm dark + sienna quill',
          swatch: ['#161310', '#211b15', '#d99a63', '#d0a35a', '#efe6d9'] },
        { id: 'dark',           name: 'Default Dark',   desc: 'Charcoal + violet, the original',
          swatch: ['#13111a', '#241f30', '#9b7dff', '#f0a050', '#f0edf5'] },
        { id: 'light',          name: 'Default Light',  desc: 'Bright neutral for daytime',
          swatch: ['#f8f6fb', '#ffffff', '#7c5cd6', '#d08030', '#1c1926'] },
        { id: 'ink_copper',     name: 'Ink & Copper',   desc: 'Slate + copper. Matches the marketing site.',
          swatch: ['#13110e', '#241f1a', '#d08136', '#e5a05a', '#f5efe4'] },
        { id: 'parchment',      name: 'Parchment',      desc: 'Warm sepia paper, brown ink',
          swatch: ['#f4ead7', '#faf3e3', '#8a4a1c', '#b8742e', '#3a2f1f'] },
        { id: 'midnight_press', name: 'Midnight Press', desc: 'True black for OLED, cool steel accents',
          swatch: ['#000000', '#121212', '#8aabc2', '#d4a574', '#f0f0f0'] },
        { id: 'forest',         name: 'Forest',         desc: 'Pine + sage + cream. Calm.',
          swatch: ['#1a2620', '#2c3e37', '#8db588', '#d8b67a', '#ecf0e8'] },
        { id: 'velvet',         name: 'Velvet',         desc: 'Aubergine, dusty rose, amber',
          swatch: ['#1a1018', '#2e1f30', '#c47a9a', '#e5a060', '#f5e8ee'] },
        { id: 'high_contrast',  name: 'High Contrast',  desc: 'Pure black/white + saturated yellow (a11y)',
          swatch: ['#000000', '#0a0a0a', '#ffeb3b', '#ff9800', '#ffffff'] },
        { id: 'retro_2005',     name: 'Retro 2005',     desc: 'Early-2000s web: silver chrome, beveled buttons, Verdana',
          swatch: ['#d4d0c8', '#ffffff', '#0a52c9', '#ff6600', '#000000'] },
    ],

    applyTheme(themeId) {
        const valid = this.THEMES.some(t => t.id === themeId);
        const id = valid ? themeId : 'quill';
        document.documentElement.dataset.theme = id;
        localStorage.setItem('pawpoller-theme', id);
        // Best-effort sync to settings.json so the choice follows the user
        // across devices when cloud sync is set up. Silent on failure —
        // localStorage already covers the local case.
        try { API.savePreferences({ theme: id }); } catch (e) { /* ignore */ }
        // Charts read CSS variables at draw time, so destroy + re-render
        // any current page so colours pick up.
        if (typeof Charts !== 'undefined' && Charts.destroyAll) Charts.destroyAll();
        if (this.route) this.route();
    },

    getCurrentTheme() {
        return document.documentElement.dataset.theme || 'quill';
    },

    /* ── Mobile mode ─────────────────────────────────────────
       Single source of truth: `<html data-mobile="0|1">`. CSS
       selectors keyed off the attribute (e.g. `html[data-mobile="1"]
       .editor-quad`) are the mobile-mode-only enhancements. The
       inline boot script in index.html sets the initial value before
       CSS evaluates so there's no flash of desktop UX on a phone.
       The mode selector below lets the user override the auto-detect:
         - 'auto'  → tracks (max-width: 768px) via matchMedia
         - 'on'    → forces "1" (mobile UX everywhere)
         - 'off'   → forces "0" (desktop UX everywhere; existing
                     `@media (max-width: 768px)` rules still fire on
                     small viewports — this only suppresses the new
                     mobile-mode-only enhancements)
       Persisted to localStorage for synchronous boot, and to
       settings.json so the choice syncs across devices. */
    MOBILE_MODES: ['auto', 'on', 'off'],

    getMobileModeOverride() {
        return localStorage.getItem('pawpoller-mobile-mode') || 'auto';
    },

    isMobileLayoutActive() {
        return document.documentElement.dataset.mobile === '1';
    },

    _resolveMobile(mode) {
        if (mode === 'on') return '1';
        if (mode === 'off') return '0';
        return window.matchMedia('(max-width: 768px)').matches ? '1' : '0';
    },

    applyMobileMode(mode) {
        const valid = this.MOBILE_MODES.includes(mode);
        const m = valid ? mode : 'auto';
        localStorage.setItem('pawpoller-mobile-mode', m);
        const prev = document.documentElement.dataset.mobile;
        const next = this._resolveMobile(m);
        document.documentElement.dataset.mobile = next;
        // Re-paint the picker cards in-place — most pages don't need a
        // full route re-render (the CSS reads `data-mobile` directly).
        // We deliberately skip route() because the editor is the most
        // sensitive surface and a re-render would cost the user any
        // unsaved CodeMirror state. Pages with layout-dependent JS
        // (charts, Publish Check matrix) handle their own redraws.
        document.querySelectorAll('#mobile-mode-picker .mobile-mode-card').forEach(card => {
            const isActive = card.dataset.mmId === m;
            card.classList.toggle('active', isActive);
            const pill = card.querySelector('.active-pill');
            if (isActive && !pill) {
                const span = document.createElement('span');
                span.className = 'active-pill';
                span.textContent = 'Active';
                card.appendChild(span);
            } else if (!isActive && pill) {
                pill.remove();
            }
        });
        // If the layout dimension actually flipped (desktop ↔ mobile),
        // redraw any charts on the current page so they re-measure.
        // Editor + most pages handle this via CSS alone.
        if (prev !== next && typeof Charts !== 'undefined' && Charts.destroyAll) {
            Charts.destroyAll();
            // Only re-route if not on the editor (which manages its own
            // CodeMirror state and would lose unsaved edits).
            if (this.route && !location.hash.startsWith('#/editor/')) {
                this.route();
            }
        }
        try { API.savePreferences({ mobile_mode: m }); } catch (e) { /* ignore */ }
    },

    /* ── Nav placement (top bar vs side rail) ────────────────
       Single source of truth: `<html data-nav="top|side">`, set by the inline
       boot script before CSS paints (no flash). Default is 'top' — the redesign
       shell. Persisted to localStorage for synchronous boot; per-device for now
       (unlike theme/mobile it isn't synced to settings.json — the top-nav CSS is
       desktop-only anyway, gated to non-mobile so phones keep their bottom nav).
       Picked in Settings → Appearance. */
    NAV_MODES: ['top', 'side'],

    getNavModeOverride() {
        return document.documentElement.dataset.navmode
            || localStorage.getItem('pawpoller-nav') || 'top';
    },

    applyNavMode(mode) {
        const m = this.NAV_MODES.includes(mode) ? mode : 'top';
        localStorage.setItem('pawpoller-nav', m);
        document.documentElement.dataset.navmode = m;
        // Re-paint the picker cards in place (same idiom as the mobile-mode picker).
        document.querySelectorAll('#nav-mode-picker .mobile-mode-card').forEach(card => {
            const isActive = card.dataset.navId === m;
            card.classList.toggle('active', isActive);
            const pill = card.querySelector('.active-pill');
            if (isActive && !pill) {
                const span = document.createElement('span');
                span.className = 'active-pill';
                span.textContent = 'Active';
                card.appendChild(span);
            } else if (!isActive && pill) {
                pill.remove();
            }
        });
        // Side rail vs full-width top bar changes the content column width, so
        // charts must re-measure. Skip the re-route on the editor (unsaved CM state).
        if (typeof Charts !== 'undefined' && Charts.destroyAll) {
            Charts.destroyAll();
            if (this.route && !location.hash.startsWith('#/editor/')) this.route();
        }
    },

    /* ── Display mode (visual skin: Default / Brut) ──────────
       Single source of truth: `<html data-mode="brut">` (attribute ABSENT =
       Default), set by the inline boot script before CSS paints (no flash).
       A display mode is a token + component-character swap layered on top of
       the active theme — it's theme-aware, so Brut reads correctly under Quill
       and every dark theme. Only the character changes (thick ink borders, hard
       offset shadows, bold-sans headings, squared avatars, press-down buttons);
       layout is untouched, so charts don't need to re-measure.
       Terminal/Console is deliberately NOT a dashboard skin — that green-on-black
       operator aesthetic belongs to the headless/Docker surface, not here (see
       docs/documentation_guide.md). Persisted to localStorage for synchronous
       boot; per-device (like nav mode — it's a look, not account data). */
    DISPLAY_MODES: ['default', 'brut'],

    getModeOverride() {
        return document.documentElement.dataset.mode
            || localStorage.getItem('pawpoller-mode') || 'default';
    },

    applyMode(mode) {
        const m = this.DISPLAY_MODES.includes(mode) ? mode : 'default';
        localStorage.setItem('pawpoller-mode', m);
        // Attribute ABSENT means Default (keeps the selector surface minimal).
        if (m === 'default') delete document.documentElement.dataset.mode;
        else document.documentElement.dataset.mode = m;
        // Re-paint the picker cards in place (same idiom as the nav-mode picker).
        document.querySelectorAll('#display-mode-picker .mobile-mode-card').forEach(card => {
            const isActive = card.dataset.modeId === m;
            card.classList.toggle('active', isActive);
            const pill = card.querySelector('.active-pill');
            if (isActive && !pill) {
                const span = document.createElement('span');
                span.className = 'active-pill';
                span.textContent = 'Active';
                card.appendChild(span);
            } else if (!isActive && pill) {
                pill.remove();
            }
        });
        // Brut swaps borders/shadows/type but not layout — the CSS reads
        // data-mode directly, so no re-route (and no lost editor state).
    },

    /* Called once at boot from init(). Watches the viewport so `auto`
       mode tracks viewport changes (rotation, window resize) without
       a page reload. No-op when the override is forced on/off. */
    _initMobileModeWatcher() {
        if (this._mobileModeWatcherBound) return;
        this._mobileModeWatcherBound = true;
        const mql = window.matchMedia('(max-width: 768px)');
        const onChange = () => {
            const mode = this.getMobileModeOverride();
            if (mode !== 'auto') return;
            const next = this._resolveMobile('auto');
            if (document.documentElement.dataset.mobile !== next) {
                document.documentElement.dataset.mobile = next;
                if (typeof Charts !== 'undefined' && Charts.destroyAll) Charts.destroyAll();
                // Skip route() on the editor — we don't want a rotation
                // to wipe unsaved CodeMirror state.
                if (this.route && !location.hash.startsWith('#/editor/')) {
                    this.route();
                }
            }
        };
        // addEventListener works on modern Safari; older Safari uses addListener
        if (mql.addEventListener) mql.addEventListener('change', onChange);
        else if (mql.addListener) mql.addListener(onChange);
    },

    async _initSidebarVersion() {
        const container = document.getElementById('sidebar-version');
        if (!container) return;
        try {
            // Resolve runtime mode once at init. The auto-update apply
            // path only works on a frozen PyInstaller .exe — server
            // (Docker) installs are updated by `pawupdate` on the host.
            // Cache the value so _renderSidebarVersion can decide
            // whether to show the apply button or a "rebuild on host"
            // hint without re-fetching every check.
            if (this._runtimeMode === undefined) {
                try {
                    const status = await API.getSetupStatus();
                    this._runtimeMode = status.runtime_mode || 'desktop';
                } catch {
                    this._runtimeMode = 'desktop';
                }
            }
            const info = await API.checkUpdate().catch(() => ({ available: false, current: '?', latest: '?' }));
            this._renderSidebarVersion(container, info);
        } catch {
            container.innerHTML = '';
        }
    },

    _renderSidebarVersion(container, info) {
        const isServer = this._runtimeMode === 'server';
        if (info.available) {
            // On server runtime the in-app apply path can't work
            // (it needs a frozen .exe + Windows batch script), so
            // show only the version banner — admin updates via
            // `pawupdate` / `docker compose up --build` on the host.
            const updateBtn = isServer
                ? `<span class="version-text" title="Update with pawupdate / docker compose --build on the host" style="font-size:10px;color:var(--text-muted)">rebuild on host</span>`
                : `<button class="btn-update-now" id="sidebar-update-btn">Update Now</button>`;
            container.innerHTML = `
                <span class="update-available">v${Utils.escapeHtml(info.latest)} available</span>
                ${updateBtn}`;
            document.getElementById('sidebar-update-btn')?.addEventListener('click', async () => {
                if (!confirm('Download and apply the update? The app will restart.')) return;
                const btn = document.getElementById('sidebar-update-btn');
                btn.disabled = true;
                btn.textContent = 'Updating...';
                try {
                    await API.applyUpdate({ download_url: info.download_url });
                    btn.textContent = 'Restarting...';
                } catch (err) {
                    btn.textContent = 'Failed';
                    alert('Update failed: ' + err.message);
                }
            });
        } else {
            container.innerHTML = `
                <span class="version-text">v${Utils.escapeHtml(info.current)}</span>
                <button class="btn-check-update" id="sidebar-check-btn">Check for Updates</button>`;
            document.getElementById('sidebar-check-btn')?.addEventListener('click', async () => {
                const btn = document.getElementById('sidebar-check-btn');
                btn.disabled = true;
                btn.textContent = 'Checking...';
                try {
                    const result = await API.checkUpdate();
                    this._renderSidebarVersion(container, result);
                } catch {
                    btn.textContent = 'Failed';
                    setTimeout(() => { btn.textContent = 'Check for Updates'; btn.disabled = false; }, 3000);
                }
            });
        }
    },

    /*
     * navigate() — Programmatic navigation helper.
     * Sets window.location.hash which triggers the hashchange listener
     * and therefore route(). Use instead of direct hash assignment for
     * readability (e.g. this.navigate('/login')).
     */
    navigate(path) {
        window.location.hash = path;
    },

    /* route() — Main SPA router. Parses hash, toggles sidebar, dispatches to renderer. */
    route() {
        this._stopAutoRefresh();

        /* Parse hash: '#/fa/submission/42' -> hash='/fa/submission/42', parts=['fa','submission','42']
           A '?key=value' tail is split off BEFORE the path is segmented (2.193.0),
           so route params never leak into the last path segment — the unified art
           detail uses '#/artwork/image/My_Art?v=nsfw' to preselect a variant, and
           without this the name would come through as 'My_Art?v=nsfw'. Read the
           tail via App.routeQuery(). No pre-2.193 route uses '?', so this is inert
           for every other page. */
        const rawHash = window.location.hash.slice(1) || '/';
        const _q = rawHash.indexOf('?');
        const hash = _q === -1 ? rawHash : rawHash.slice(0, _q);
        this._routeQuery = _q === -1 ? '' : rawHash.slice(_q + 1);
        const parts = hash.split('/').filter(Boolean);

        /* Full-screen pages hide the sidebar, context bar, bottom nav, and
           remove the main column's left margin. */
        const isFullScreen = parts[0] === 'login' || parts[0] === 'loading'
            || parts[0] === 'dashboard-login' || parts[0] === 'dashboard-setup'
            || parts[0] === 'setup';
        const sidebar = document.querySelector('.sidebar');
        const mainCol = document.getElementById('main-col');
        const bottomNav = document.getElementById('bottom-nav');
        if (sidebar) sidebar.style.display = isFullScreen ? 'none' : '';
        if (mainCol) mainCol.style.marginLeft = isFullScreen ? '0' : '';
        if (bottomNav) bottomNav.style.display = isFullScreen ? 'none' : '';

        /* A "platform route" is the Platforms hub or any platform code
           (#/{code}, #/{code}/submissions, …). On these the "Platforms" nav
           item is the active one. The top-level #/submissions is NOT a platform
           route — it's the cross-platform Submissions hub. */
        const platformCodes = (window.PLATFORMS || []).map(p => p.code);
        const isPlatformRoute = parts[0] === 'platforms'
            || platformCodes.includes(parts[0]);

        /* Tint the platform detail page-header with that platform's brand
           colour (light bold-pass). #main-col survives #app re-renders, so
           setting the attribute + var here lets redesign.css style the
           eventually-rendered .page-header without touching the per-platform
           render functions. The hub (#/platforms) is not a single platform. */
        const _pcode = platformCodes.includes(parts[0]) ? parts[0] : null;
        if (mainCol) {
            if (_pcode) {
                mainCol.dataset.platform = _pcode;
                mainCol.style.setProperty('--page-accent', `var(--platform-${_pcode})`);
            } else {
                delete mainCol.dataset.platform;
                mainCol.style.removeProperty('--page-accent');
            }
        }

        /* Highlight the active sidebar nav link */
        document.querySelectorAll('.nav-link').forEach(link => {
            const href = link.getAttribute('href');
            let active = href === '#' + hash || (hash === '/' && href === '#/');
            if (isPlatformRoute && href === '#/platforms') active = true;
            /* Persona overview pages live under Accounts — keep it lit. */
            if (parts[0] === 'persona' && href === '#/accounts') active = true;
            /* The Stories and Artwork hubs are gone (2.155.0) — their records live in
               Library, so their surviving sub-pages light "Library" instead:
                 - story detail (#/posting/story/...), but NOT queue/log, which are
                   their own Insights & Tools items;
                 - artwork detail/log/ignored, but NOT #/artwork/new, which is the
                   Create → New Artwork item.
               The bare #/posting and #/artwork routes redirect, so they never land
               here — these cover only the sub-routes. */
            if (!active && parts[0] === 'posting' && parts[1] && parts[1] !== 'queue'
                && parts[1] !== 'log' && href === '#/library') active = true;
            if (!active && parts[0] === 'artwork' && parts[1] && parts[1] !== 'new'
                && parts[1] !== 'quick' && href === '#/library') active = true;
            /* Posts sub-pages (contacts) keep "Posts" (Publish) lit; #/posts/new is
               the Create → New Post item, so it keeps its own highlight. */
            if (!active && parts[0] === 'posts' && parts[1] && parts[1] !== 'new' && href === '#/posts') active = true;
            /* #/submissions/discovered is the standalone discovered page; light Library
               (its hub lost its nav entry in 2.117.0 and its root now redirects). */
            if (!active && parts[0] === 'submissions' && href === '#/library') active = true;
            /* Library (Bookshelf) work sub-routes (#/library/work/...) keep "Library" lit.
               Masterpieces live inside Library (grid + #/masterpieces/{name} detail), so
               keep "Library" lit for those routes too. */
            if (!active && (parts[0] === 'library' || parts[0] === 'masterpieces') && href === '#/library') active = true;
            link.classList.toggle('active', active);
        });

        /* Render the breadcrumb / platform-switcher / sub-tab context bar */
        this._renderContextBar(parts, isFullScreen);

        /* Update bottom nav active state */
        if (bottomNav) {
            bottomNav.querySelectorAll('.bottom-nav-item[data-page]').forEach(item => {
                const page = item.dataset.page;
                let on;
                if (page === 'overview') on = (hash === '/' || parts[0] === '' || parts[0] === 'overview');
                else if (page === 'platforms') on = isPlatformRoute;
                else on = parts[0] === page;
                item.classList.toggle('active', on);
            });
        }

        /* Destroy old Chart.js instances to free canvas memory */
        Charts.destroyAll();

        /* Guided tours — after the page's renderer runs, give the tour engine
           a chance to auto-fire. It self-gates: getting-started fires once on
           the overview; a page's own tour fires once on first visit, but only
           after getting-started is done. maybeAuto() no-ops cheaply when the
           relevant flag is already set, so calling it on every route is fine.
           The captured hash guards against firing for a page you've left. */
        {
            const _tourHash = window.location.hash;
            setTimeout(() => window.Tour?.maybeAuto(_tourHash), 120);
        }

        /* One-shot "What's new" check — fires on the first authenticated page.
         * Skipped on the auth-gate screens (the /api/whatsnew fetch is auth-gated
         * and would 401 before login); runs once per session. */
        if (!this._whatsNewChecked
            && !['dashboard-login', 'dashboard-setup', 'setup', 'login', 'loading'].includes(parts[0])) {
            this._whatsNewChecked = true;
            this._maybeShowWhatsNew();
        }

        if (parts[0] === 'dashboard-login') {
            this.renderDashboardLogin();
        } else if (parts[0] === 'dashboard-setup') {
            this.renderDashboardSetup();
        } else if (parts[0] === 'setup') {
            // 2.16.13 (BUG-017): hard-block #/setup once setup_complete
            // is true so users can't accidentally re-enter the wizard
            // and overwrite live config. The "Re-run setup" button
            // clears the flag server-side first, so it still flows
            // through this guard cleanly.
            this._guardSetupRoute();
        } else if (parts[0] === 'login') {
            this.renderLogin();
        } else if (parts[0] === 'loading') {
            this.renderLoading();
        } else if (hash === '/' || hash === '' || parts[0] === 'overview') {
            this.renderOverview();
        } else if (parts[0] === 'platforms') {
            this.renderPlatformsHub();
        } else if (parts[0] === 'ib' && !parts[1]) {
            this.renderDashboard();
        } else if (parts[0] === 'ib' && parts[1] === 'submissions' && !parts[2]) {
            this.renderSubmissions();
        } else if (parts[0] === 'ib' && parts[1] === 'submission' && parts[2]) {
            this.renderDetail(parseInt(parts[2]));
        } else if (parts[0] === 'ib' && parts[1] === 'compare') {
            this.renderCompare();
        } else if (parts[0] === 'fa' && (!parts[1] || parts[1] === '')) {
            this.renderFADashboard();
        } else if (parts[0] === 'fa' && parts[1] === 'submissions' && !parts[2]) {
            this.renderFASubmissions();
        } else if (parts[0] === 'fa' && parts[1] === 'submission' && parts[2]) {
            this.renderFADetail(parseInt(parts[2]));
        } else if (parts[0] === 'fa' && parts[1] === 'compare') {
            this.renderFACompare();
        } else if (parts[0] === 'ws' && (!parts[1] || parts[1] === '')) {
            this.renderWSDashboard();
        } else if (parts[0] === 'ws' && parts[1] === 'submissions' && !parts[2]) {
            this.renderWSSubmissions();
        } else if (parts[0] === 'ws' && parts[1] === 'submission' && parts[2]) {
            this.renderWSDetail(parseInt(parts[2]));
        } else if (parts[0] === 'ws' && parts[1] === 'compare') {
            this.renderWSCompare();
        } else if (parts[0] === 'sf' && (!parts[1] || parts[1] === '')) {
            this.renderSFDashboard();
        } else if (parts[0] === 'sf' && parts[1] === 'submissions' && !parts[2]) {
            this.renderSFSubmissions();
        } else if (parts[0] === 'sf' && parts[1] === 'submission' && parts[2]) {
            this.renderSFDetail(parts[2]);
        } else if (parts[0] === 'sf' && parts[1] === 'compare') {
            this.renderSFCompare();
        } else if (parts[0] === 'sqw' && (!parts[1] || parts[1] === '')) {
            this.renderSQWDashboard();
        } else if (parts[0] === 'sqw' && parts[1] === 'submissions' && !parts[2]) {
            this.renderSQWSubmissions();
        } else if (parts[0] === 'sqw' && parts[1] === 'submission' && parts[2]) {
            this.renderSQWDetail(parseInt(parts[2]));
        } else if (parts[0] === 'sqw' && parts[1] === 'compare') {
            this.renderSQWCompare();
        } else if (parts[0] === 'ao3' && (!parts[1] || parts[1] === '')) {
            this.renderAO3Dashboard();
        } else if (parts[0] === 'ao3' && parts[1] === 'submissions' && !parts[2]) {
            this.renderAO3Submissions();
        } else if (parts[0] === 'ao3' && parts[1] === 'submission' && parts[2]) {
            this.renderAO3Detail(parseInt(parts[2]));
        } else if (parts[0] === 'ao3' && parts[1] === 'compare') {
            this.renderAO3Compare();
        } else if (parts[0] === 'da' && (!parts[1] || parts[1] === '')) {
            this.renderDADashboard();
        } else if (parts[0] === 'da' && parts[1] === 'submissions' && !parts[2]) {
            this.renderDASubmissions();
        } else if (parts[0] === 'da' && parts[1] === 'submission' && parts[2]) {
            this.renderDADetail(parseInt(parts[2]));
        } else if (parts[0] === 'da' && parts[1] === 'compare') {
            this.renderDACompare();
        } else if (parts[0] === 'wp' && (!parts[1] || parts[1] === '')) {
            this.renderWPDashboard();
        } else if (parts[0] === 'wp' && parts[1] === 'submissions' && !parts[2]) {
            this.renderWPSubmissions();
        } else if (parts[0] === 'wp' && parts[1] === 'submission' && parts[2]) {
            this.renderWPDetail(parseInt(parts[2]));
        } else if (parts[0] === 'wp' && parts[1] === 'compare') {
            this.renderWPCompare();
        } else if (parts[0] === 'ik' && (!parts[1] || parts[1] === '')) {
            this.renderIKDashboard();
        } else if (parts[0] === 'ik' && parts[1] === 'submissions' && !parts[2]) {
            this.renderIKSubmissions();
        } else if (parts[0] === 'ik' && parts[1] === 'submission' && parts[2]) {
            this.renderIKDetail(parseInt(parts[2]));
        } else if (parts[0] === 'ik' && parts[1] === 'compare') {
            this.renderIKCompare();
        } else if (parts[0] === 'bsky' && (!parts[1] || parts[1] === '')) {
            this.renderBSKYDashboard();
        } else if (parts[0] === 'bsky' && parts[1] === 'submissions' && !parts[2]) {
            this.renderBSKYSubmissions();
        } else if (parts[0] === 'bsky' && parts[1] === 'submission' && parts[2]) {
            this.renderBSKYDetail(parts[2]);
        } else if (parts[0] === 'bsky' && parts[1] === 'compare') {
            this.renderBSKYCompare();
        } else if (parts[0] === 'mast' && (!parts[1] || parts[1] === '')) {
            this.renderMASTDashboard();
        } else if (parts[0] === 'mast' && parts[1] === 'submissions' && !parts[2]) {
            this.renderMASTSubmissions();
        } else if (parts[0] === 'mast' && parts[1] === 'submission' && parts[2]) {
            this.renderMASTDetail(parts[2]);
        } else if (parts[0] === 'mast' && parts[1] === 'compare') {
            this.renderMASTCompare();
        } else if (parts[0] === 'tum' && (!parts[1] || parts[1] === '')) {
            this.renderTUMDashboard();
        } else if (parts[0] === 'tum' && parts[1] === 'submissions' && !parts[2]) {
            this.renderTUMSubmissions();
        } else if (parts[0] === 'tum' && parts[1] === 'submission' && parts[2]) {
            this.renderTUMDetail(parts[2]);
        } else if (parts[0] === 'tum' && parts[1] === 'compare') {
            this.renderTUMCompare();
        } else if (parts[0] === 'pix' && (!parts[1] || parts[1] === '')) {
            this.renderPIXDashboard();
        } else if (parts[0] === 'pix' && parts[1] === 'submissions' && !parts[2]) {
            this.renderPIXSubmissions();
        } else if (parts[0] === 'pix' && parts[1] === 'submission' && parts[2]) {
            this.renderPIXDetail(parts[2]);
        } else if (parts[0] === 'pix' && parts[1] === 'compare') {
            this.renderPIXCompare();
        } else if (parts[0] === 'thr' && (!parts[1] || parts[1] === '')) {
            this.renderTHRDashboard();
        } else if (parts[0] === 'thr' && parts[1] === 'submissions' && !parts[2]) {
            this.renderTHRSubmissions();
        } else if (parts[0] === 'thr' && parts[1] === 'submission' && parts[2]) {
            this.renderTHRDetail(parts[2]);
        } else if (parts[0] === 'thr' && parts[1] === 'compare') {
            this.renderTHRCompare();
        } else if (parts[0] === 'ig' && (!parts[1] || parts[1] === '')) {
            this.renderIGDashboard();
        } else if (parts[0] === 'ig' && parts[1] === 'submissions' && !parts[2]) {
            this.renderIGSubmissions();
        } else if (parts[0] === 'ig' && parts[1] === 'submission' && parts[2]) {
            this.renderIGDetail(parts[2]);
        } else if (parts[0] === 'ig' && parts[1] === 'compare') {
            this.renderIGCompare();
        } else if (parts[0] === 'e621' && (!parts[1] || parts[1] === '')) {
            this.renderE621Dashboard();
        } else if (parts[0] === 'e621' && parts[1] === 'submissions' && !parts[2]) {
            this.renderE621Submissions();
        } else if (parts[0] === 'e621' && parts[1] === 'submission' && parts[2]) {
            this.renderE621Detail(parts[2]);
        } else if (parts[0] === 'e621' && parts[1] === 'compare') {
            this.renderE621Compare();
        } else if (parts[0] === 'fn' && (!parts[1] || parts[1] === '')) {
            this.renderFNDashboard();
        } else if (parts[0] === 'fn' && parts[1] === 'submissions' && !parts[2]) {
            this.renderFNSubmissions();
        } else if (parts[0] === 'fn' && parts[1] === 'submission' && parts[2]) {
            this.renderFNDetail(parts[2]);
        } else if (parts[0] === 'fn' && parts[1] === 'compare') {
            this.renderFNCompare();
        } else if (parts[0] === 'fbr' && (!parts[1] || parts[1] === '')) {
            this.renderFBRDashboard();
        } else if (parts[0] === 'fbr' && parts[1] === 'submissions' && !parts[2]) {
            this.renderFBRSubmissions();
        } else if (parts[0] === 'fbr' && parts[1] === 'submission' && parts[2]) {
            this.renderFBRDetail(parts[2]);
        } else if (parts[0] === 'fbr' && parts[1] === 'compare') {
            this.renderFBRCompare();
        } else if (parts[0] === 'tw' && (!parts[1] || parts[1] === '')) {
            this.renderTWDashboard();
        } else if (parts[0] === 'tw' && parts[1] === 'submissions' && !parts[2]) {
            this.renderTWSubmissions();
        } else if (parts[0] === 'tw' && parts[1] === 'submission' && parts[2]) {
            this.renderTWDetail(parts[2]);
        } else if (parts[0] === 'tw' && parts[1] === 'compare') {
            this.renderTWCompare();
        } else if (parts[0] === 'groups' && !parts[1]) {
            this.renderGroups();
        } else if (parts[0] === 'group' && parts[1]) {
            this.renderGroupDetail(parseInt(parts[1]));
        } else if (parts[0] === 'cross-platform') {
            // Retired in 2.113.0 — folded into Collections. Redirect old links.
            location.hash = '#/collections';
            return;
        } else if (parts[0] === 'inbox') {
            window.Inbox.render();
        } else if (parts[0] === 'analytics') {
            this.renderAnalytics();
        } else if (parts[0] === 'repost-radar') {
            this.renderRepostRadar();
        } else if (parts[0] === 'accounts') {
            if (window.Accounts) window.Accounts.render();
        } else if (parts[0] === 'persona' && parts[1]) {
            if (window.Accounts) window.Accounts.renderPersonaDetail(parseInt(parts[1]));
        } else if (parts[0] === 'settings') {
            this.renderSettings();
        } else if (parts[0] === 'posting' && !parts[1]) {
            // Stories hub RETIRED (2.155.0, backlog L) — it was /api/works filtered
            // to stories with no search/sort, i.e. a strict subset of the Library's
            // Stories segment. Redirect rather than render: old links, bookmarks and
            // the bottom nav all point here. Story DETAIL (#/posting/story/{name})
            // is untouched — merging hubs doesn't merge the pages behind them.
            window.location.replace('#/library/type/story');
        } else if (parts[0] === 'posting' && parts[1] === 'story' && parts[2]) {
            // Story name may contain slashes (e.g. My_Story/Alt_Version)
            Posting.renderStoryDetail(parts.slice(2).join('/'));
        } else if (parts[0] === 'posting' && parts[1] === 'queue') {
            Posting.renderQueue();
        } else if (parts[0] === 'posting' && parts[1] === 'published') {
            Posting.renderPublished();
        } else if (parts[0] === 'posting' && parts[1] === 'log') {
            Posting.renderLog();
        } else if (parts[0] === 'editor' && !parts[1]) {
            Editor.renderStoryList();
        } else if (parts[0] === 'editor' && parts[1]) {
            // Story name may contain slashes (e.g. My_Story/Nice_Version)
            Editor.renderEditor(parts.slice(1).join('/'));
        } else if (parts[0] === 'promo') {
            if (window.Promo) window.Promo.render();
        } else if (parts[0] === 'imagetool') {
            if (window.ImageTool) window.ImageTool.render();
        } else if (parts[0] === 'artwork' && !parts[1]) {
            // Artwork hub RETIRED (2.155.0, backlog L) — its library grid was
            // /api/works filtered to artwork, and its discovered tiles are now the
            // Library's Discovered segment. Artwork's other pages (new / detail /
            // log / ignored) all survive below.
            window.location.replace('#/library/type/artwork');
        } else if (parts[0] === 'artwork' && parts[1] === 'quick') {
            if (window.Artwork) window.Artwork.renderQuick();
        } else if (parts[0] === 'artwork' && parts[1] === 'new') {
            if (window.Artwork) window.Artwork.renderUpload();
        } else if (parts[0] === 'artwork' && parts[1] === 'image' && parts[2]) {
            // Artwork name may contain slashes — rejoin the tail.
            if (window.Artwork) window.Artwork.renderDetail(parts.slice(2).join('/'));
        } else if (parts[0] === 'artwork' && parts[1] === 'log') {
            if (window.Artwork) window.Artwork.renderLog();
        } else if (parts[0] === 'artwork' && parts[1] === 'ignored') {
            if (window.Artwork) window.Artwork.renderIgnored();
        } else if (parts[0] === 'posts' && parts[1] === 'contacts') {
            if (window.Posts) window.Posts.renderContacts();
        } else if (parts[0] === 'posts' && parts[1] === 'new') {
            if (window.Posts) window.Posts.renderCompose();
        } else if (parts[0] === 'posts') {
            if (window.Posts) window.Posts.render();
        } else if (parts[0] === 'submissions' && parts[1] === 'triage') {
            if (window.Submissions) window.Submissions.renderTriage();
        } else if (parts[0] === 'submissions' && parts[1] === 'discovered') {
            if (window.Submissions) window.Submissions.renderDiscovered();
        } else if (parts[0] === 'submissions') {
            // The Submissions hub lost its nav entry in 2.117.0 but kept rendering
            // for anyone with the URL — a fourth works hub reachable by accident.
            // "One works hub" (2.155.0, backlog L) has to mean by URL too.
            window.location.replace('#/library');
        } else if (parts[0] === 'collections' && parts[1]) {
            if (window.Collections) window.Collections.renderDetail(parts[1]);
        } else if (parts[0] === 'collections') {
            if (window.Collections) window.Collections.render();
        } else if (parts[0] === 'artists') {
            if (window.Artists) window.Artists.render();
        } else if (parts[0] === 'commissions' && parts[1] === 'archived') {
            if (window.Commissions) window.Commissions.render(true);
        } else if (parts[0] === 'commissions' && parts[1]) {
            if (window.Commissions) window.Commissions.renderDetail(parts[1]);
        } else if (parts[0] === 'commissions') {
            if (window.Commissions) window.Commissions.render();
        } else if (parts[0] === 'masterpieces' && parts[1] === 'duplicates') {
            // Duplicate-finder / merge review (2.144.0).
            if (window.Masterpieces) window.Masterpieces.renderDuplicates();
        } else if (parts[0] === 'masterpieces' && parts[1]) {
            // Masterpiece detail (read-only, Phase 2). Name may contain slashes.
            if (window.Masterpieces) window.Masterpieces.renderDetail(parts.slice(1).join('/'));
        } else if (parts[0] === 'masterpieces') {
            // The managed grid lives inside Library under its own type filter —
            // land there with the Masterpieces segment pre-selected.
            if (window.Bookshelf) { window.Bookshelf._type = 'masterpiece'; window.Bookshelf.render(); }
        } else if (parts[0] === 'library' && parts[1] === 'discovered') {
            // Discovered-art bucket, moved under Library (Submissions retired, 2.117.0).
            // Still its own page — it's also a Library segment (2.155.0), but this
            // route is linked from elsewhere and bookmarked, so it keeps working.
            if (window.Submissions) window.Submissions.renderDiscovered();
        } else if (parts[0] === 'library' && parts[1] === 'type' && parts[2]) {
            // Deep-link a Library segment (2.155.0) — how the retired Stories and
            // Artwork hubs land here, and what the segment bar writes to the URL.
            if (window.Bookshelf) {
                window.Bookshelf._type = window.Bookshelf.TYPES.includes(parts[2]) ? parts[2] : 'all';
                window.Bookshelf.render();
            }
        } else if (parts[0] === 'library' && parts[1] === 'sort' && parts[2]) {
            // Deep-link to the shelf pre-sorted by a metric (Overview stat cards).
            if (window.Bookshelf) { window.Bookshelf._sort = parts[2]; window.Bookshelf.render(); }
        } else if (parts[0] === 'library' && parts[1] === 'work' && parts[2]) {
            // Work name may contain slashes — rejoin the tail.
            if (window.Bookshelf) window.Bookshelf.renderWork(parts.slice(2).join('/'));
        } else if (parts[0] === 'library' && parts[1] === 'browse') {
            // The CLASSIC full grid (filters/search/sorts) — the Showcase's
            // "✕ Classic view" target. Reset the segment (see bare-route note).
            if (window.Bookshelf) { window.Bookshelf._type = 'all'; window.Bookshelf.render(); }
        } else if (parts[0] === 'library') {
            // Bare #/library opens in the view the user LAST CHOSE (2.158.0):
            // classic grid by default; the Showcase shelves once they've hit
            // "▤ Shelf view" (and "✕ Classic view" switches back). The choice
            // persists per-browser in localStorage — an option, never forced.
            let pref = 'classic';
            try { pref = localStorage.getItem('pp_library_view') || 'classic'; } catch { /* default */ }
            if (pref === 'shelves' && window.Showcase) {
                window.Showcase.renderLibrary();
            } else if (window.Bookshelf) {
                window.Bookshelf._type = 'all';
                window.Bookshelf.render();
            }
        } else if (parts[0] === 'laurels') {
            if (window.Laurels) window.Laurels.render();
        } else if (parts[0] === 'ledger') {
            if (window.Ledger) window.Ledger.render();
        } else if (parts[0] === 'getting-started') {
            if (window.Guides) window.Guides.renderHub();
        } else {
            this._setContent('<div class="empty-state"><h3>Page not found</h3></div>');
        }
    },

    /* _renderContextBar() — fills #context-bar with a breadcrumb and, when
     * inside a platform, a platform switcher + Dashboard/Submissions/Compare
     * sub-tabs. Driven entirely by the parsed route at the shell level, so
     * none of the per-page render functions need to know about it. Left
     * empty on full-screen routes (CSS hides an empty bar). */
    _renderContextBar(parts, isFullScreen) {
        const bar = document.getElementById('context-bar');
        if (!bar) return;
        if (isFullScreen) { bar.innerHTML = ''; return; }

        const p0 = parts[0] || '';
        const codes = (window.PLATFORMS || []).map(p => p.code);

        /* Resolve platform + sub-view. All platforms (incl. Inkbunny, 2.68.0)
           use prefixed routes: #/{code}, #/{code}/submissions, etc. */
        let platform = null, sub = 'dash';
        if (codes.includes(p0)) {
            platform = p0;
            if (parts[1] === 'submissions') sub = 'subs';
            else if (parts[1] === 'compare') sub = 'compare';
            else if (parts[1] === 'submission') sub = 'detail';
        }

        if (platform) {
            bar.innerHTML = this._platformContextBar(platform, sub);
            const sel = document.getElementById('ctx-platform-switch');
            sel?.addEventListener('change', () => {
                const route = window.platformRoute || ((c) => '#/' + c);
                window.location.hash = route(sel.value);
            });
            this._populateAccountSwitch(platform);
            return;
        }

        /* On mobile, skip the context bar for non-platform pages — it would
           only repeat the page <h2> and sit under the fixed hamburger. */
        if (document.documentElement.dataset.mobile === '1') { bar.innerHTML = ''; return; }

        /* Non-platform pages: a simple breadcrumb for orientation. */
        const labels = {
            '': 'Overview', overview: 'Overview', platforms: 'Platforms',
            library: 'Library', posting: 'Stories', editor: 'Story Editor',
            analytics: 'Analytics', inbox: 'Inbox', laurels: 'Laurels', ledger: 'Activity', groups: 'Groups', 'cross-platform': 'Cross-Platform',
            accounts: 'Accounts', settings: 'Settings',
        };
        let crumb;
        if (p0 === 'posting' && parts[1] === 'queue') {
            crumb = '<a href="#/library">Library</a> <span class="sep">›</span> <span class="here">Queue</span>';
        } else if (p0 === 'posting' && parts[1] === 'log') {
            crumb = '<a href="#/library">Library</a> <span class="sep">›</span> <span class="here">History</span>';
        } else if (p0 === 'posting' && parts[1] === 'story') {
            crumb = '<a href="#/library/type/story">Library</a> <span class="sep">›</span> <span class="here">Story</span>';
        } else if (p0 === 'library' && parts[1] === 'work') {
            crumb = '<a href="#/library">Library</a> <span class="sep">›</span> <span class="here">Work</span>';
        } else if (p0 === 'editor' && parts[1]) {
            crumb = '<a href="#/editor">Story Editor</a> <span class="sep">›</span> <span class="here">Editing</span>';
        } else if (p0 === 'group' && parts[1]) {
            crumb = '<a href="#/groups">Groups</a> <span class="sep">›</span> <span class="here">Group</span>';
        } else {
            crumb = '<span class="here">' + (labels[p0] || 'Overview') + '</span>';
        }
        bar.innerHTML = '<div class="ctx-crumbs">' + crumb + '</div>';
    },

    /* Build the breadcrumb + sub-tabs + switcher HTML for a platform route. */
    _platformContextBar(code, sub) {
        const plat = window.platformByCode ? window.platformByCode(code) : null;
        const label = plat ? plat.label : code.toUpperCase();
        const emoji = plat ? plat.emoji : '';
        const color = plat ? plat.color : 'var(--accent)';
        const route = window.platformRoute || ((c, s) => s ? '#/' + c + '/' + s : '#/' + c);
        const subName = sub === 'subs' ? 'Submissions'
            : sub === 'compare' ? 'Compare'
            : sub === 'detail' ? 'Submission' : 'Dashboard';

        const crumb = '<a href="#/platforms">Platforms</a> <span class="sep">›</span> '
            + '<a href="' + route(code) + '">' + label + '</a> '
            + '<span class="sep">›</span> <span class="here">' + subName + '</span>';

        const tab = (key, name, sname) =>
            '<a href="' + route(code, sname) + '" class="' + (sub === key ? 'active' : '') + '">' + name + '</a>';
        const subtabs = '<div class="ctx-subtabs">'
            + tab('dash', 'Dashboard', '')
            + tab('subs', 'Submissions', 'submissions')
            + tab('compare', 'Compare', 'compare')
            + '</div>';

        const options = (window.PLATFORMS || []).map(p =>
            '<option value="' + p.code + '"' + (p.code === code ? ' selected' : '') + '>' + p.label + '</option>'
        ).join('');
        const switcher = '<div class="ctx-switch"><span class="pe" style="color:' + color + '">' + emoji + '</span>'
            + '<select id="ctx-platform-switch" aria-label="Switch platform">' + options + '</select></div>';

        // Account-filter slot — populated async by _populateAccountSwitch (only
        // shows a <select> when the platform has 2+ enabled accounts).
        const acctSlot = '<span id="ctx-account-slot" class="ctx-account"></span>';

        return '<div class="ctx-crumbs">' + crumb + '</div>' + subtabs + acctSlot + switcher;
    },

    /* Current account filter for a platform code (null = All accounts). */
    _acctId(code) { return (this._accountFilter && this._accountFilter[code]) || null; },

    /* Populate a platform dashboard's follower widget (current count + growth
     * chart) from /api/followers/:platform. Async + best-effort: it injects
     * itself right after the dashboard's main .stats-grid, so a missing, slow, or
     * unsupported follower fetch never blocks or reshapes the dashboard. Called
     * once per dashboard render (auto-refresh re-runs it against the fresh DOM). */
    async _loadFollowerWidget(platform, accountId) {
        let data;
        try { data = await API.getFollowers(platform, { account_id: accountId }); }
        catch (e) { return; }
        if (!data || !data.supported || data.followers == null) return;
        const grid = document.getElementById('app') && document.getElementById('app').querySelector('.stats-grid');
        if (!grid || !grid.parentNode || grid.parentNode.querySelector('.follower-widget')) return;
        const series = data.series || [];
        const hasChart = series.length >= 2;
        const section = document.createElement('div');
        section.className = 'follower-widget';
        section.innerHTML = `
            <div class="stats-grid" style="margin-top:16px">
                ${Components.statCard('Followers', data.followers)}
            </div>
            ${hasChart ? `<div class="chart-container"><h3>Follower Growth</h3><div class="chart-wrap"><canvas id="chart-followers-${platform}"></canvas></div></div>` : ''}
        `;
        grid.parentNode.insertBefore(section, grid.nextSibling);
        if (hasChart) Charts.aggregateLine('chart-followers-' + platform, series, ['followers']);
    },

    /* _populateAccountSwitch() — async-fills the context bar's account slot with
     * an "All accounts" + per-account <select>, but only when the platform has
     * 2+ enabled accounts. Changing it sets this._accountFilter[code] and
     * re-renders the current platform view (scoped). */
    async _populateAccountSwitch(code) {
        this._accountFilter = this._accountFilter || {};
        const slot = document.getElementById('ctx-account-slot');
        if (!slot) return;
        let accts;
        try {
            const data = await API.getAccounts(code);
            accts = (data.accounts || []).filter(a => a.enabled);
        } catch (e) { return; }
        if (!accts || accts.length < 2) return;       // single account → no selector
        if (!document.body.contains(slot)) return;     // a newer render replaced it
        const cur = this._accountFilter[code];
        const esc = (s) => String(s == null ? '' : s).replace(/[&<>"']/g, c => (
            { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
        const opts = ['<option value="">All accounts</option>'].concat(
            accts.map(a => '<option value="' + a.account_id + '"'
                + (String(cur) === String(a.account_id) ? ' selected' : '') + '>'
                + esc(a.label || a.handle || ('Account ' + a.account_id)) + '</option>')
        ).join('');
        slot.innerHTML = '<select id="ctx-account-switch" aria-label="Filter by account" title="Account">' + opts + '</select>';
        document.getElementById('ctx-account-switch').addEventListener('change', (e) => {
            const v = e.target.value;
            this._accountFilter[code] = v === '' ? null : Number(v);
            this.route();   // re-dispatch to the platform render, now scoped
        });
    },

    /* _setContent() — DOM helper: replaces the #app main content area with the given HTML string. */
    _setContent(html) {
        document.getElementById('app').innerHTML = html;
    },

    /* _loading() — DOM helper: shows a spinner placeholder while async data loads. */
    _loading() {
        this._setContent('<div class="loading-spinner">Loading...</div>');
    },

    /* _loadLogs() — Fetch and display application log file in the Logs settings tab. */
    async _loadLogs() {
        const output = document.getElementById('log-output');
        const info = document.getElementById('log-info');
        if (!output) return;
        const file = document.getElementById('log-file-select')?.value || 'server';
        const lines = document.getElementById('log-lines-select')?.value || '200';
        const autoScroll = document.getElementById('log-auto-scroll')?.checked !== false;
        output.textContent = 'Loading...';
        try {
            const data = await API.getLogs({ file, lines });
            if (data.lines && data.lines.length > 0) {
                output.textContent = data.lines.join('\n');
            } else {
                output.textContent = '(no log entries)';
            }
            if (info) info.textContent = `Showing ${data.lines?.length || 0} of ${data.total_lines || 0} lines from ${file}.log`;
            if (autoScroll) output.scrollTop = output.scrollHeight;
        } catch (err) {
            output.textContent = `Error loading logs: ${err.message}`;
        }
    },

    /* _loadCFProxyToggles() — Render the per-platform opt-in CF proxy
     * checkboxes. Pulls current values from /api/settings/preferences,
     * writes back via POST on toggle. */
    async _loadCFProxyToggles() {
        const root = document.getElementById('cf-proxy-toggles');
        if (!root) return;
        const platforms = [
            { key: 'ib',   name: 'Inkbunny' },
            { key: 'fa',   name: 'FurAffinity' },
            { key: 'ws',   name: 'Weasyl' },
            { key: 'sqw',  name: 'SquidgeWorld' },
            { key: 'ao3',  name: 'AO3' },
            { key: 'bsky', name: 'Bluesky' },
            { key: 'ik',   name: 'Itaku' },
            { key: 'wp',   name: 'Wattpad' },
            { key: 'tw',   name: 'X / Twitter' },
            { key: 'mast', name: 'Mastodon' },
            { key: 'tum',  name: 'Tumblr' },
            { key: 'pix',  name: 'Pixiv' },
            { key: 'thr',  name: 'Threads' },
            { key: 'ig',   name: 'Instagram' },
        ];
        try {
            const prefs = await API.getPreferences();
            const haveCfWorker = !!prefs.cf_worker_configured;
            const rows = platforms.map(p => {
                const settingKey = `${p.key}_use_cf_proxy`;
                const checked = !!prefs[settingKey];
                const dis = haveCfWorker ? '' : 'disabled';
                return `<label class="settings-row" style="display:flex;align-items:center;gap:10px;padding:6px 0;${haveCfWorker ? '' : 'opacity:0.55;'}">
                    <input type="checkbox" data-cf-platform="${p.key}" ${checked ? 'checked' : ''} ${dis}>
                    <span class="settings-label" style="flex:1">${p.name}</span>
                    <span style="font-size:11px;color:var(--text-muted);">${settingKey}</span>
                </label>`;
            }).join('');
            const banner = haveCfWorker
                ? ''
                : `<p style="font-size:12px;color:#d66;margin:0 0 10px;">CF Worker URL/key not configured — toggles disabled. Set <code>cf_worker_url</code> and <code>cf_worker_key</code> in settings.json first.</p>`;
            root.innerHTML = banner + rows;
            root.querySelectorAll('input[type="checkbox"][data-cf-platform]').forEach(cb => {
                cb.addEventListener('change', async () => {
                    const platform = cb.dataset.cfPlatform;
                    const key = `${platform}_use_cf_proxy`;
                    cb.disabled = true;
                    try {
                        await API.savePreferences({ [key]: cb.checked });
                    } catch (e) {
                        cb.checked = !cb.checked;
                    } finally {
                        cb.disabled = false;
                    }
                });
            });
        } catch (e) {
            root.innerHTML = `<p style="color:#d66;font-size:12px;">Failed to load proxy settings: ${e.message}</p>`;
        }
    },

    /* _loadPollingTab() — Lazy-load platform polling status and logs when
     * the Polling settings tab is activated. Fetches IB status + poll log,
     * plus each connected platform's status + poll log in parallel. */
    async _loadPollingTab() {
        const container = document.getElementById('polling-platforms-container');
        if (!container) return;
        // Always (re)load the proxy toggles when the tab opens — they're
        // independent of the platform-status loading.
        this._loadCFProxyToggles();
        if (this._pollingTabLoaded) return; // already loaded this render
        container.innerHTML = '<div style="text-align:center;padding:40px;color:var(--text-muted)">Loading polling data...</div>';
        try {
            const auth = this._pollingAuth || {};
            const platforms = [
                { key: 'fa', auth: auth.faAuth?.has_cookies, name: 'FurAffinity', statusFn: 'getFAStatus', logFn: 'getFAPollLog', tableFn: 'faPollLogTable' },
                { key: 'ws', auth: auth.wsAuth?.has_key, name: 'Weasyl', statusFn: 'getWSStatus', logFn: 'getWSPollLog', tableFn: 'wsPollLogTable' },
                { key: 'sf', auth: auth.sfAuth?.has_credentials, name: 'SoFurry', statusFn: 'getSFStatus', logFn: 'getSFPollLog', tableFn: 'sfPollLogTable' },
                { key: 'sqw', auth: auth.sqwAuth?.has_credentials, name: 'SquidgeWorld', statusFn: 'getSQWStatus', logFn: 'getSQWPollLog', tableFn: 'sqwPollLogTable' },
                { key: 'ao3', auth: auth.ao3Auth?.has_credentials, name: 'AO3', statusFn: 'getAO3Status', logFn: 'getAO3PollLog', tableFn: 'ao3PollLogTable' },
                { key: 'da', auth: auth.daAuth?.has_credentials, name: 'DeviantArt', statusFn: 'getDAStatus', logFn: 'getDAPollLog', tableFn: 'daPollLogTable' },
                { key: 'wp', auth: auth.wpAuth?.has_credentials, name: 'Wattpad', statusFn: 'getWPStatus', logFn: 'getWPPollLog', tableFn: 'wpPollLogTable' },
                { key: 'ik', auth: auth.ikAuth?.has_credentials, name: 'Itaku', statusFn: 'getIKStatus', logFn: 'getIKPollLog', tableFn: 'ikPollLogTable' },
                { key: 'bsky', auth: auth.bskyAuth?.has_credentials, name: 'Bluesky', statusFn: 'getBSKYStatus', logFn: 'getBSKYPollLog', tableFn: 'bskyPollLogTable' },
                { key: 'mast', auth: auth.mastAuth?.has_credentials, name: 'Mastodon', statusFn: 'getMASTStatus', logFn: 'getMASTPollLog', tableFn: 'mastPollLogTable' },
                { key: 'tum', auth: auth.tumAuth?.has_credentials, name: 'Tumblr', statusFn: 'getTUMStatus', logFn: 'getTUMPollLog', tableFn: 'tumPollLogTable' },
                { key: 'pix', auth: auth.pixAuth?.has_credentials, name: 'Pixiv', statusFn: 'getPIXStatus', logFn: 'getPIXPollLog', tableFn: 'pixPollLogTable' },
                { key: 'thr', auth: auth.thrAuth?.has_credentials, name: 'Threads', statusFn: 'getTHRStatus', logFn: 'getTHRPollLog', tableFn: 'thrPollLogTable' },
                { key: 'ig', auth: auth.igAuth?.has_credentials, name: 'Instagram', statusFn: 'getIGStatus', logFn: 'getIGPollLog', tableFn: 'igPollLogTable' },
                { key: 'tw', auth: auth.twAuth?.has_credentials, name: 'Twitter', statusFn: 'getTWStatus', logFn: 'getTWPollLog', tableFn: 'twPollLogTable' },
                { key: 'e621', auth: auth.e621Auth?.has_credentials, name: 'e621', statusFn: 'getE621Status', logFn: 'getE621PollLog', tableFn: 'e621PollLogTable' },
                { key: 'fn', auth: auth.fnAuth?.has_credentials, name: 'FurryNetwork', statusFn: 'getFNStatus', logFn: 'getFNPollLog', tableFn: 'fnPollLogTable' },
                { key: 'fbr', auth: auth.fbrAuth?.has_credentials, name: 'Furbooru', statusFn: 'getFBRStatus', logFn: 'getFBRPollLog', tableFn: 'fbrPollLogTable' },
            ];
            // Alphabetical by name (Inkbunny is rendered separately, first).
            platforms.sort((a, b) => a.name.toLowerCase().localeCompare(b.name.toLowerCase()));
            const connected = platforms.filter(p => p.auth);

            // Build fetch array: IB status + log, then each connected platform's status + log
            const fetches = [
                API.getStatus().catch(() => ({ total_submissions: 0, total_snapshots: 0, last_poll: null })),
                API.getPollLog(20).catch(() => ({ polls: [] })),
            ];
            for (const p of connected) {
                fetches.push(API[p.statusFn]().catch(() => ({})));
                fetches.push(API[p.logFn](20).catch(() => ({ polls: [] })));
            }
            // Per-platform pause state (2.103.0) — appended last so the idx=2
            // status/log pairing below stays intact.
            const pausedIdx = fetches.length;
            fetches.push(API.getPollPaused().catch(() => ({ paused_platforms: [] })));

            const results = await Promise.all(fetches);
            const ibStatus = results[0];
            const ibPollLog = results[1];
            const lastPoll = ibPollLog.polls?.[0] || null;
            const pausedSet = new Set((results[pausedIdx]?.paused_platforms) || []);
            // Renders the right pause/resume button for a platform code.
            const pauseBtn = (key) => pausedSet.has(key)
                ? `<button class="btn btn-sm btn-secondary" data-resume-plat="${key}">▶ Resume polling</button>`
                : `<button class="btn btn-sm btn-warning" data-pause-plat="${key}">⏸ Pause polling</button>`;
            const pausedTag = (key) => pausedSet.has(key)
                ? ' <span class="summary-meta" style="color:var(--warning)">· paused</span>' : '';

            // Map platform results
            const pData = {};
            let idx = 2;
            for (const p of connected) {
                pData[p.key] = { status: results[idx], pollLog: results[idx + 1] };
                idx += 2;
            }

            // Build Inkbunny accordion (grid wrapper opens here, 2.103.0)
            let html = `<div class="polling-grid">
                <details class="settings-accordion">
                    <summary><span class="status-dot ${lastPoll?.status === 'success' ? 'connected' : 'disconnected'}"></span>Inkbunny <span class="summary-meta">— ${lastPoll ? lastPoll.status + ' \u00b7 ' + Utils.formatDateTime(lastPoll.started_at) : 'Never polled'}</span>${pausedTag('ib')}</summary>
                    <div class="accordion-body">
                    <div class="settings-row"><span class="settings-label">Submissions tracked</span><span class="settings-value">${ibStatus.total_submissions}</span></div>
                    <div class="settings-row"><span class="settings-label">Snapshots stored</span><span class="settings-value">${Utils.formatNumber(ibStatus.total_snapshots)}</span></div>
                    <div class="settings-row"><span class="settings-label">Last poll</span><span class="settings-value">${lastPoll ? Utils.formatDateTime(lastPoll.started_at) : 'Never'}</span></div>
                    <div class="settings-row"><span class="settings-label">Last poll status</span><span class="settings-value" style="color:${lastPoll?.status === 'success' ? 'var(--success)' : lastPoll?.status === 'error' ? 'var(--danger)' : 'var(--text-primary)'}">${lastPoll?.status || '--'}</span></div>
                    ${lastPoll?.error_message ? `<div class="settings-row"><span class="settings-label">Last error</span><span class="settings-value" style="color:var(--danger)">${Utils.escapeHtml(lastPoll.error_message)}</span></div>` : ''}
                    <div style="margin-top:12px;display:flex;gap:8px;flex-wrap:wrap">
                        <button class="btn btn-sm btn-primary" data-poll="ib">Poll Now</button>
                        <button class="btn btn-sm btn-secondary" data-resync="ib">Full Resync</button>
                        ${pauseBtn('ib')}
                    </div>
                    <div style="margin-top:12px">${Components.pollLogTable(ibPollLog.polls)}</div>
                    </div>
                </details>`;

            // Build each connected platform accordion
            for (const p of connected) {
                const ps = pData[p.key].status;
                const pl = pData[p.key].pollLog;
                const lp = ps.last_poll || null;
                html += `
                <details class="settings-accordion">
                    <summary><span class="status-dot ${lp?.status === 'success' ? 'connected' : 'disconnected'}"></span>${p.name} <span class="summary-meta">— ${lp ? lp.status + ' \u00b7 ' + Utils.formatDateTime(lp.started_at) : 'Never polled'}</span>${pausedTag(p.key)}</summary>
                    <div class="accordion-body">
                    <div class="settings-row"><span class="settings-label">Submissions tracked</span><span class="settings-value">${ps.total_submissions || 0}</span></div>
                    <div class="settings-row"><span class="settings-label">Snapshots stored</span><span class="settings-value">${Utils.formatNumber(ps.total_snapshots || 0)}</span></div>
                    <div class="settings-row"><span class="settings-label">Last poll</span><span class="settings-value">${lp ? Utils.formatDateTime(lp.started_at) : 'Never'}</span></div>
                    <div class="settings-row"><span class="settings-label">Last poll status</span><span class="settings-value" style="color:${lp?.status === 'success' ? 'var(--success)' : lp?.status === 'error' ? 'var(--danger)' : 'var(--text-primary)'}">${lp?.status || '--'}</span></div>
                    ${lp?.error_message ? `<div class="settings-row"><span class="settings-label">Last error</span><span class="settings-value" style="color:var(--danger)">${Utils.escapeHtml(lp.error_message)}</span></div>` : ''}
                    <div style="margin-top:12px;display:flex;gap:8px;flex-wrap:wrap">
                        <button class="btn btn-sm btn-primary" data-poll="${p.key}">Poll Now</button>
                        <button class="btn btn-sm btn-secondary" data-resync="${p.key}">Full Resync</button>
                        ${pauseBtn(p.key)}
                    </div>
                    <div style="margin-top:12px">${this._pollLogTable(p)(pl.polls)}</div>
                    </div>
                </details>`;
            }
            html += `</div>`; // close .polling-grid (2.103.0)

            container.innerHTML = html;
            this._pollingTabLoaded = true;
        } catch (err) {
            container.innerHTML = `<div style="text-align:center;padding:40px;color:var(--danger)">Failed to load polling data: ${Utils.escapeHtml(err.message)}</div>`;
        }
    },

    /* Resolve a platform's poll-log renderer, degrading to a one-row notice
       rather than taking the whole tab down.

       The platform list above names `Components[tableFn]` for 18 platforms and
       nothing checks that the function exists — one fact, several declarations,
       no check. FurryNetwork shipped without its renderer (2.200.0) and the
       resulting TypeError was caught by the outer try/catch, so the entire
       Polling tab rendered "Failed to load polling data:
       Components[p.tableFn] is not a function" — every other platform's status
       lost to one missing function. A missing renderer is now a missing TABLE,
       which is what it actually is. (3.17.1) */
    _pollLogTable(p) {
        const fn = Components[p.tableFn];
        if (typeof fn === 'function') return fn.bind(Components);
        console.warn(`No poll-log renderer for ${p.key} (${p.tableFn})`);
        return () => `<p style="color:var(--text-muted)">No poll-log view for `
            + `${Utils.escapeHtml(p.name)} yet.</p>`;
    },

    /* ── Per-Platform Poll/Resync helpers (used by dashboard headers + polling tab) */

    // Display labels for toast messages — "sf" → "SoFurry" reads better
    // than the raw platform code in user-facing notifications.
    _platformLabels: {
        ib: 'Inkbunny', fa: 'FurAffinity', ws: 'Weasyl', sf: 'SoFurry',
        sqw: 'SquidgeWorld', ao3: 'AO3', da: 'DeviantArt', wp: 'Wattpad',
        ik: 'Itaku', bsky: 'Bluesky', tw: 'X/Twitter', mast: 'Mastodon', tum: 'Tumblr', pix: 'Pixiv', thr: 'Threads', ig: 'Instagram', e621: 'e621', fn: 'FurryNetwork', fbr: 'Furbooru',
    },

    /* Settings → Platforms → "Session health" card. Renders the per-platform
     * active-session status (from /api/platforms/sessions) and wires the
     * "Check sessions now" button, which fires the server-side re-validation
     * (fire-and-forget) and reloads the results after it's had time to run. */
    async _initSessionHealthCard() {
        const CHECKABLE = ['ao3', 'sf', 'sqw', 'bsky', 'mast', 'tum', 'pix', 'thr', 'ig', 'e621', 'fn', 'fbr'];
        const LABELS = (window.PlatformHealth && window.PlatformHealth.LABELS) || {};
        const DOT = { valid: 'connected', expired: 'disconnected', error: 'warn', unconfigured: 'muted' };
        const WORD = { valid: 'Valid', expired: 'Expired', error: 'Unverified', unconfigured: 'Not configured' };

        const renderList = (sessions, credReport) => {
            const list = document.getElementById('session-status-list');
            if (!list) return;
            const sessionRows = CHECKABLE.map((code) => {
                const s = sessions[code];
                const status = (s && s.status) || 'unchecked';
                const dot = DOT[status] || 'muted';
                const word = WORD[status] || 'Not checked yet';
                const when = (s && s.checked_at && window.PlatformHealth)
                    ? ` · ${window.PlatformHealth.relativePast(s.checked_at)}` : '';
                const detail = ((status === 'expired' || status === 'error') && s && s.detail)
                    ? `<div class="ssl-detail">${Utils.escapeHtml(s.detail)}</div>` : '';
                return `<div class="ssl-row">
                    <span class="status-dot ${dot}"></span>
                    <span class="ssl-name">${Utils.escapeHtml(LABELS[code] || code.toUpperCase())}</span>
                    <span class="ssl-status">${word}${when}</span>
                    ${detail}
                </div>`;
            }).join('');
            // Proactive credential-age warnings (W): cookie/token logins that
            // don't auto-refresh (X/FA/DA) getting close to their typical lifetime
            // — flagged BEFORE they fail, unlike the session dots above.
            const warns = (credReport || []).filter(r => r.level !== 'ok');
            const credRows = warns.length ? (
                `<div class="ssl-row" style="margin-top:.5rem;opacity:.75;font-size:.78rem;letter-spacing:.03em;text-transform:uppercase;">
                    <span class="ssl-name">Login age — reconnect before these expire</span></div>`
                + warns.map(r => {
                    const label = this._platformLabels[r.code] || r.code.toUpperCase();
                    const age = r.age_days != null ? `~${r.age_days} day${r.age_days === 1 ? '' : 's'} old` : 'aging';
                    const msg = r.level === 'stale'
                        ? `${age}, past its ~${r.ttl_days}-day typical lifetime — reconnect to avoid a break`
                        : `${age} — may expire soon, reconnect when convenient`;
                    return `<div class="ssl-row">
                        <span class="status-dot ${r.level === 'stale' ? 'disconnected' : 'warn'}"></span>
                        <span class="ssl-name">${Utils.escapeHtml(label)}</span>
                        <span class="ssl-status">&#8987; ${Utils.escapeHtml(msg)}</span>
                    </div>`;
                }).join('')
            ) : '';
            list.innerHTML = sessionRows + credRows;
            // Summary dot on the accordion header: red if any expired, amber if
            // any unverified, else green.
            const dotEl = document.getElementById('session-health-dot');
            if (dotEl) {
                const states = CHECKABLE.map((c) => sessions[c] && sessions[c].status);
                dotEl.className = 'status-dot ' + (states.includes('expired') ? 'disconnected'
                    : states.includes('error') ? 'warn' : 'connected');
            }
        };

        const load = async () => {
            try {
                const [resp, cred] = await Promise.all([
                    API.getPlatformSessions(),
                    API.getCredentialAge().catch(() => ({ report: [] })),
                ]);
                renderList((resp && resp.sessions) || {}, (cred && cred.report) || []);
            } catch (e) { /* leave the placeholder */ }
        };

        await load();

        const btn = document.getElementById('session-check-btn');
        const msg = document.getElementById('session-check-msg');
        btn?.addEventListener('click', async () => {
            btn.disabled = true;
            btn.textContent = 'Checking…';
            if (msg) msg.textContent = 'Validating every session — a few seconds…';
            try {
                await API.triggerSessionCheck();
            } catch (err) {
                btn.disabled = false;
                btn.textContent = 'Check sessions now';
                if (msg) msg.textContent = 'Failed: ' + (err.message || err);
                return;
            }
            // Server validates all 8 serially in the background; reload once
            // partway and once when they've all had time to land.
            setTimeout(load, 4000);
            setTimeout(async () => {
                await load();
                btn.disabled = false;
                btn.textContent = 'Check sessions now';
                if (msg) msg.textContent = 'Updated.';
                if (window.PlatformHealth) window.PlatformHealth.fetchOnce();
            }, 9000);
        });
    },

    /* Fresh list of configured platform codes for the fan-out Poll Now / Full
     * Resync buttons. Reads /platforms/health at CLICK time so a stale
     * _pollingAuth snapshot can't silently drop a configured platform (2.95.0).
     * Falls back to the cached snapshot only if the health fetch fails/empty. */
    async _configuredPollCodes() {
        const ALL = ['ib', 'fa', 'ws', 'sf', 'sqw', 'ao3', 'da', 'wp', 'ik',
            'bsky', 'tw', 'mast', 'tum', 'pix', 'thr', 'ig', 'e621', 'fn', 'fbr'];
        try {
            const health = await API.getPlatformsHealth();
            if (health && typeof health === 'object') {
                const codes = ALL.filter(c => health[c] && health[c].configured);
                if (codes.length) return codes;
            }
        } catch (e) { /* fall through to the cached snapshot */ }
        const a = this._pollingAuth || {};
        const cached = { ib: true, fa: a.faAuth?.has_cookies, ws: a.wsAuth?.has_key,
            sf: a.sfAuth?.has_credentials, sqw: a.sqwAuth?.has_credentials, ao3: a.ao3Auth?.has_credentials,
            da: a.daAuth?.has_credentials, wp: a.wpAuth?.has_credentials, ik: a.ikAuth?.has_credentials,
            bsky: a.bskyAuth?.has_credentials, tw: a.twAuth?.has_credentials, mast: a.mastAuth?.has_credentials,
            tum: a.tumAuth?.has_credentials, pix: a.pixAuth?.has_credentials, thr: a.thrAuth?.has_credentials,
            ig: a.igAuth?.has_credentials, e621: a.e621Auth?.has_credentials,
            fn: a.fnAuth?.has_credentials, fbr: a.fbrAuth?.has_credentials };
        return ALL.filter(c => cached[c]);
    },

    async _dashPoll(btn, platform) {
        const label = this._platformLabels[platform] || platform.toUpperCase();
        btn.disabled = true;
        btn.textContent = 'Polling...';
        /* Honour the context-bar account switcher: if a specific account is
         * selected for this platform, poll just that one; otherwise ("All
         * accounts", or a single-account platform) poll every enabled account.
         * The generic /api/poll/trigger/{code} endpoint handles both. */
        const acct = (this._accountFilter && this._accountFilter[platform] != null)
            ? this._accountFilter[platform] : null;
        try {
            await API.triggerAccountPoll(platform, acct);
            btn.textContent = 'Done!';
            const scope = acct != null ? 'selected account' : 'all accounts';
            if (window.toast) window.toast.success(`${label}: poll triggered (${scope})`);
            /* Poll is fire-and-forget (2.51.5) — there's no fresh data 1.5 s
             * later, and a full this.route() re-render bounced the viewport to
             * the top of the page (2.51.7). Just reset the button; the 60 s
             * auto-refresh picks up new counts with scroll preserved. */
            setTimeout(() => { btn.textContent = 'Poll Now'; btn.disabled = false; }, 2000);
        } catch (err) {
            btn.textContent = 'Error';
            if (window.toast) window.toast.error(`${label}: poll failed — ${err.message || err}`);
            setTimeout(() => { btn.textContent = 'Poll Now'; btn.disabled = false; }, 2000);
        }
    },

    async _dashResync(btn, platform) {
        const label = this._platformLabels[platform] || platform.toUpperCase();
        if (!confirm(`Full resync re-fetches every ${label} submission from scratch. This can take several minutes and will hit ${label}'s rate limits hard. Continue?`)) return;
        btn.disabled = true;
        btn.textContent = 'Syncing...';
        const fns = { ib: 'fullResync', fa: 'fullFAResync', ws: 'fullWSResync', sf: 'fullSFResync', sqw: 'fullSQWResync', ao3: 'fullAO3Resync', da: 'fullDAResync', wp: 'fullWPResync', ik: 'fullIKResync', bsky: 'fullBSKYResync', tw: 'fullTWResync', mast: 'fullMASTResync', tum: 'fullTUMResync', pix: 'fullPIXResync', thr: 'fullTHRResync', ig: 'fullIGResync', e621: 'fullE621Resync', fn: 'fullFNResync', fbr: 'fullFBRResync' };
        try {
            await API[fns[platform]]();
            btn.textContent = 'Done!';
            if (window.toast) window.toast.success(`${label}: full resync triggered (may take several minutes)`);
            /* Same as _dashPoll: skip the full re-render (scroll jump, 2.51.7) —
             * auto-refresh handles freshness. Just reset the button. */
            setTimeout(() => { btn.textContent = 'Full Resync'; btn.disabled = false; }, 2000);
        } catch (err) {
            btn.textContent = 'Error';
            if (window.toast) window.toast.error(`${label}: resync failed — ${err.message || err}`);
            setTimeout(() => { btn.textContent = 'Full Resync'; btn.disabled = false; }, 2000);
        }
    },

    /* Per-platform pause toggle (2.103.0). `paused === true` pauses; false
     * resumes. The scheduler skips paused platforms on its 240-min cycle;
     * manual "Poll Now" / "Full Resync" still work. Updates the button in
     * place (no full re-render → no scroll jump). */
    async _togglePlatformPause(btn, platform, paused) {
        const label = this._platformLabels[platform] || platform.toUpperCase();
        btn.disabled = true;
        btn.textContent = paused ? 'Pausing…' : 'Resuming…';
        try {
            if (paused) await API.pausePlatformPolling(platform);
            else await API.resumePlatformPolling(platform);
            // Flip the button to its opposite state in place.
            if (paused) {
                btn.dataset.pausePlat = undefined; delete btn.dataset.pausePlat;
                btn.setAttribute('data-resume-plat', platform);
                btn.textContent = '▶ Resume polling';
                btn.classList.remove('btn-warning'); btn.classList.add('btn-secondary');
            } else {
                delete btn.dataset.resumePlat;
                btn.setAttribute('data-pause-plat', platform);
                btn.textContent = '⏸ Pause polling';
                btn.classList.remove('btn-secondary'); btn.classList.add('btn-warning');
            }
            btn.disabled = false;
            if (window.toast) window.toast.success(`${label}: scheduled polling ${paused ? 'paused' : 'resumed'}`);
        } catch (err) {
            btn.textContent = 'Error';
            if (window.toast) window.toast.error(`${label}: ${paused ? 'pause' : 'resume'} failed — ${err.message || err}`);
            setTimeout(() => { btn.textContent = paused ? '⏸ Pause polling' : '▶ Resume polling'; btn.disabled = false; }, 2000);
        }
    },

    /* Settings search (2.103.0). Filters across every settings tab at once:
     * the searchable unit is a `.settings-section` or a top-level
     * `.settings-accordion` (per-platform cards live in Platforms/Polling).
     * A non-empty query shows all panels and hides units whose text doesn't
     * match; an empty query restores the normal active-tab-only view. Lazy
     * tabs (Polling / Logs) are eager-loaded the first time a search runs so
     * their content is searchable too. */
    _wireSettingsSearch(tabBar) {
        const input = document.getElementById('settings-search');
        const info = document.getElementById('settings-search-info');
        if (!input || !tabBar) return;

        // Collect the searchable units in a panel: sections, plus any
        // accordions that aren't already inside a section (avoids double work).
        const unitsIn = (panel) => {
            const units = Array.from(panel.querySelectorAll('.settings-section'));
            panel.querySelectorAll('.settings-accordion').forEach(a => {
                if (!a.closest('.settings-section')) units.push(a);
            });
            return units;
        };

        const restore = () => {
            const active = tabBar.querySelector('.settings-tab.active')?.dataset.stab || 'general';
            tabBar.style.display = '';
            document.querySelectorAll('.settings-tab-content').forEach(p => {
                p.style.display = p.dataset.tabContent === active ? '' : 'none';
            });
            document.querySelectorAll('.settings-tab-content .settings-section, .settings-tab-content .settings-accordion')
                .forEach(u => { u.style.display = ''; });
            if (info) info.textContent = '';
        };

        const run = () => {
            const q = input.value.trim().toLowerCase();
            if (!q) { restore(); return; }
            // Eager-load lazy tabs once so their content is searchable.
            if (!this._settingsSearchLoadedLazy) {
                this._settingsSearchLoadedLazy = true;
                try { this._loadPollingTab(); } catch (e) { /* ignore */ }
                try { this._loadLogs(); } catch (e) { /* ignore */ }
                // Re-run shortly so freshly-loaded async content is filtered too.
                setTimeout(run, 400);
            }
            tabBar.style.display = 'none';
            let matches = 0;
            document.querySelectorAll('.settings-tab-content').forEach(panel => {
                const units = unitsIn(panel);
                let panelHit = false;
                if (units.length) {
                    units.forEach(u => {
                        const hit = u.textContent.toLowerCase().includes(q);
                        u.style.display = hit ? '' : 'none';
                        if (hit) { panelHit = true; matches++; }
                    });
                } else {
                    // Panels with no discrete units (e.g. diagnostics mount).
                    panelHit = panel.textContent.toLowerCase().includes(q);
                    if (panelHit) matches++;
                }
                panel.style.display = panelHit ? '' : 'none';
            });
            if (info) info.textContent = matches
                ? `${matches} match${matches === 1 ? '' : 'es'} across all tabs`
                : 'No matching settings';
        };

        input.addEventListener('input', run);
        input.addEventListener('keydown', (e) => {
            if (e.key === 'Escape') { input.value = ''; restore(); input.blur(); }
        });
    },

    /* ── Settings → Polling tab handlers (per-platform card buttons)
     * Each platform's card in the Polling tab has its own poll/resync
     * button with an inline ``{p}-msg`` element for error display.
     * The helpers below collapse 10 × 2 = 20 near-identical handlers
     * down to two functions; before this, each platform had ~14 lines
     * of copy-pasted boilerplate. The inline msg element is preserved
     * for in-card error context; the toast adds out-of-card success
     * feedback. */
    async _pollingTabPoll({ btn, msgId, platform, apiMethod }) {
        const label = this._platformLabels[platform] || platform.toUpperCase();
        const msg = msgId ? document.getElementById(msgId) : null;
        btn.disabled = true;
        btn.textContent = 'Polling...';
        if (msg) msg.textContent = '';
        try {
            await API[apiMethod]();
            btn.textContent = 'Done!';
            if (window.toast) window.toast.success(`${label}: poll triggered`);
            setTimeout(() => this.renderSettings(), 1500);
        } catch (err) {
            btn.textContent = 'Error';
            if (msg) { msg.textContent = err.message; msg.style.color = 'var(--danger)'; }
            if (window.toast) window.toast.error(`${label}: poll failed — ${err.message || err}`);
            setTimeout(() => this.renderSettings(), 2000);
        }
    },

    async _pollingTabResync({ btn, msgId, platform, apiMethod }) {
        const label = this._platformLabels[platform] || platform.toUpperCase();
        if (!confirm(`Full resync re-fetches every ${label} submission from scratch. This can take several minutes and will hit ${label}'s rate limits hard. Continue?`)) return;
        const msg = msgId ? document.getElementById(msgId) : null;
        btn.disabled = true;
        btn.textContent = 'Syncing...';
        if (msg) msg.textContent = '';
        try {
            await API[apiMethod]();
            btn.textContent = 'Done!';
            if (window.toast) window.toast.success(`${label}: full resync triggered (allow several minutes)`);
            setTimeout(() => this.renderSettings(), 1500);
        } catch (err) {
            btn.textContent = 'Error';
            if (msg) { msg.textContent = err.message; msg.style.color = 'var(--danger)'; }
            if (window.toast) window.toast.error(`${label}: resync failed — ${err.message || err}`);
            setTimeout(() => this.renderSettings(), 2000);
        }
    },

    /* ── Dashboard Login Screen ───────────────────────────────
     * renderDashboardLogin() — Full-screen login for the self-hosted dashboard
     * auth system.  Shown when dashboard auth is configured (bcrypt hash exists)
     * and the user has no valid session cookie.  Supports optional TOTP 2FA
     * and Cloudflare Turnstile bot protection.  On success, the server sets a
     * pp_session cookie and we re-init the app. */

    async renderDashboardLogin() {
        if (this._statusCheckInterval) {
            clearInterval(this._statusCheckInterval);
            this._statusCheckInterval = null;
        }

        // Use cached dashboard status from init(), fall back to fresh fetch
        let totpEnabled = false, turnstileSiteKey = '';
        try {
            const status = this._dashboardStatus || await API.getDashboardStatus();
            totpEnabled = status.totp_enabled;
            turnstileSiteKey = status.turnstile_site_key || '';
        } catch { /* proceed with defaults */ }

        this._setContent(`
            <div class="login-screen">
                <div class="login-card">
                    <h2>PawPoller</h2>
                    <p style="color:var(--text-muted);margin-bottom:20px;font-size:13px">Sign in to access your dashboard.</p>
                    <div class="login-field">
                        <label>Username</label>
                        <input type="text" id="dash-login-username" class="search-input" placeholder="Username" style="width:100%" autocomplete="username">
                    </div>
                    <div class="login-field">
                        <label>Password</label>
                        <input type="password" id="dash-login-password" class="search-input" placeholder="Password" style="width:100%" autocomplete="current-password">
                    </div>
                    ${totpEnabled ? `
                    <div class="login-field">
                        <label>2FA Code</label>
                        <input type="text" id="dash-login-totp" class="search-input" placeholder="6-digit code" style="width:100%" inputmode="numeric" autocomplete="one-time-code" maxlength="6">
                    </div>` : ''}
                    <div id="dash-turnstile-container"></div>
                    <label class="login-remember">
                        <input type="checkbox" id="dash-login-remember" checked>
                        <span>Remember me (30 days)</span>
                    </label>
                    <button class="btn btn-primary login-btn" id="dash-login-submit">Sign In</button>
                    <div class="login-error" id="dash-login-error"></div>
                </div>
            </div>
        `);

        // Load Turnstile widget if configured
        let turnstileWidgetId = null;
        if (turnstileSiteKey) {
            const container = document.getElementById('dash-turnstile-container');
            container.style.cssText = 'margin:12px 0;display:flex;justify-content:center';
            const tsUrl = 'https://challenges.cloudflare.com/turnstile/v0/api.js?onload=_ppTurnstileReady';
            window._ppTurnstileReady = () => {
                turnstileWidgetId = window.turnstile.render('#dash-turnstile-container', {
                    sitekey: turnstileSiteKey,
                    theme: document.documentElement.dataset.theme || 'quill',
                });
            };
            // Avoid injecting duplicate script tags on repeat visits
            if (!document.querySelector(`script[src^="https://challenges.cloudflare.com/turnstile"]`)) {
                const script = document.createElement('script');
                script.src = tsUrl;
                script.async = true;
                document.head.appendChild(script);
            } else if (window.turnstile) {
                // Script already loaded — render widget directly
                window._ppTurnstileReady();
            }
        }

        const submit = async () => {
            const btn = document.getElementById('dash-login-submit');
            const errEl = document.getElementById('dash-login-error');
            const username = document.getElementById('dash-login-username').value.trim();
            const password = document.getElementById('dash-login-password').value;
            const remember = document.getElementById('dash-login-remember').checked;
            const totpCode = document.getElementById('dash-login-totp')?.value.trim() || '';

            if (!username || !password) {
                errEl.textContent = 'Username and password are required.';
                return;
            }

            btn.disabled = true;
            btn.textContent = 'Signing in...';
            errEl.textContent = '';

            const payload = { username, password, remember };
            if (totpCode) payload.totp_code = totpCode;

            // Get Turnstile token if widget is active
            if (turnstileSiteKey && window.turnstile) {
                try {
                    payload.turnstile_token = window.turnstile.getResponse(turnstileWidgetId);
                } catch { /* no token */ }
            }

            try {
                await API.dashboardLogin(payload);
                // Re-init the app — session cookie is now set
                this.init();
            } catch (err) {
                let msg = err.message.replace(/^API \d+:\s*/, '');
                try { msg = JSON.parse(msg).detail || msg; } catch {}
                errEl.textContent = msg;
                btn.textContent = 'Sign In';
                btn.disabled = false;
                // Reset Turnstile widget on failure
                if (turnstileSiteKey && window.turnstile) {
                    try { window.turnstile.reset(turnstileWidgetId); } catch {}
                }
            }
        };

        document.getElementById('dash-login-submit').addEventListener('click', submit);
        document.getElementById('dash-login-password').addEventListener('keydown', (e) => {
            if (e.key === 'Enter') {
                const totpInput = document.getElementById('dash-login-totp');
                if (totpInput) totpInput.focus();
                else submit();
            }
        });
        document.getElementById('dash-login-totp')?.addEventListener('keydown', (e) => {
            if (e.key === 'Enter') submit();
        });
        document.getElementById('dash-login-username').addEventListener('keydown', (e) => {
            if (e.key === 'Enter') document.getElementById('dash-login-password').focus();
        });
        document.getElementById('dash-login-username').focus();
    },

    /* ── Dashboard Setup Screen ───────────────────────────────
     * renderDashboardSetup() — First-time password setup for dashboard auth.
     * Only accessible when no auth is configured.  Creates a new admin user
     * with a bcrypt-hashed password.  Redirects to login on success. */

    renderDashboardSetup() {
        this._setContent(`
            <div class="login-screen">
                <div class="login-card">
                    <h2>PawPoller Setup</h2>
                    <p style="color:var(--text-muted);margin-bottom:20px;font-size:13px">Set up dashboard authentication. This is optional for desktop use.</p>
                    <div class="login-field">
                        <label>Username</label>
                        <input type="text" id="setup-username" class="search-input" placeholder="admin" value="admin" style="width:100%">
                    </div>
                    <div class="login-field">
                        <label>Password</label>
                        <input type="password" id="setup-password" class="search-input" placeholder="Minimum 8 characters" style="width:100%">
                    </div>
                    <div class="login-field">
                        <label>Confirm Password</label>
                        <input type="password" id="setup-confirm" class="search-input" placeholder="Confirm password" style="width:100%">
                    </div>
                    <button class="btn btn-primary login-btn" id="setup-submit">Create Account</button>
                    <div class="login-error" id="setup-error"></div>
                </div>
            </div>
        `);

        const submit = async () => {
            const btn = document.getElementById('setup-submit');
            const errEl = document.getElementById('setup-error');
            const username = document.getElementById('setup-username').value.trim() || 'admin';
            const password = document.getElementById('setup-password').value;
            const confirm = document.getElementById('setup-confirm').value;

            if (!password) { errEl.textContent = 'Password is required.'; return; }
            if (password.length < 8) { errEl.textContent = 'Password must be at least 8 characters.'; return; }
            if (password !== confirm) { errEl.textContent = 'Passwords do not match.'; return; }

            btn.disabled = true;
            btn.textContent = 'Creating...';
            errEl.textContent = '';

            try {
                await API.dashboardSetup({ username, password, confirm });
                this.navigate('/dashboard-login');
            } catch (err) {
                let msg = err.message.replace(/^API \d+:\s*/, '');
                try { msg = JSON.parse(msg).detail || msg; } catch {}
                errEl.textContent = msg;
                btn.textContent = 'Create Account';
                btn.disabled = false;
            }
        };

        document.getElementById('setup-submit').addEventListener('click', submit);
        document.getElementById('setup-confirm').addEventListener('keydown', (e) => {
            if (e.key === 'Enter') submit();
        });
        document.getElementById('setup-username').focus();
    },

    /* ── Setup Wizard ─────────────────────────────────────────
     * renderSetupWizard() — First-run guided experience.
     *
     * Branching by runtime + chosen mode:
     *
     *   server runtime (Docker container):
     *     1 Welcome -> 3 Archive -> 4 Platforms -> 5 Done
     *     (no mode question — server is always 'server')
     *
     *   desktop runtime, standalone:
     *     1 Welcome -> 2 Mode -> 3 Archive -> 4 Platforms -> 5 Done
     *
     *   desktop runtime, paired_desktop:
     *     1 Welcome -> 2 Mode -> 2b Pairing -> 5 Done
     *     (archive path + platform creds get pulled from the server in
     *      the pairing step, no need to ask twice)
     *
     * On completion, marks setup_complete=true and redirects to the
     * main dashboard. The polling-owner gate in main.py reads
     * setup_mode at startup, so a fresh restart applies. */

    /* 2.16.13 (BUG-017): #/setup route guard. If setup_complete is
     * already true (the normal post-onboarding state), bounce the
     * user back to overview instead of letting them re-enter the
     * wizard — accidental re-entry would let them overwrite live
     * config (archive path, platform credentials, polling owner).
     * The "Re-run setup" button on Settings clears setup_complete
     * server-side first, so it still flows through this guard. */
    async _guardSetupRoute() {
        try {
            const status = await API.getSetupStatus();
            if (status.setup_complete) {
                window.location.hash = '#/';
                this.route();
                return;
            }
        } catch (err) {
            /* If the status check fails, fall through to the wizard
             * — better to render than to strand the user on a blank
             * page. The wizard's own actions will fail noisily if
             * the backend is truly down. */
            console.warn('[App] Setup-route guard: status check failed, allowing wizard:', err);
        }
        this.renderSetupWizard();
    },

    async renderSetupWizard() {
        /* Platform definitions for the platform-connect step — reuses emoji + colour from the nav grid */
        const platforms = [
            { key: 'ib', name: 'Inkbunny', emoji: '&#128062;', color: 'var(--platform-ib)', url: 'https://inkbunny.net/login.php' },
            { key: 'fa', name: 'FurAffinity', emoji: '&#129418;', color: 'var(--platform-fa)', url: 'https://www.furaffinity.net/login/' },
            { key: 'ws', name: 'Weasyl', emoji: '&#129422;', color: 'var(--platform-ws)', url: 'https://www.weasyl.com/signin' },
            { key: 'sf', name: 'SoFurry', emoji: '&#128220;', color: 'var(--platform-sf)', url: 'https://www.sofurry.com/user/login' },
            { key: 'sqw', name: 'SquidgeWorld', emoji: '&#129433;', color: 'var(--platform-sqw)', url: 'https://squidgeworld.org/users/login' },
            { key: 'ao3', name: 'AO3', emoji: '&#128214;', color: 'var(--platform-ao3)', url: 'https://archiveofourown.org/users/login' },
            { key: 'da', name: 'DeviantArt', emoji: '&#127912;', color: 'var(--platform-da)', url: 'https://www.deviantart.com/users/login' },
            { key: 'wp', name: 'Wattpad', emoji: '&#128211;', color: 'var(--platform-wp)', url: 'https://www.wattpad.com/login' },
            { key: 'ik', name: 'Itaku', emoji: '&#128444;', color: 'var(--platform-ik)', url: 'https://itaku.ee/login' },
            { key: 'bsky', name: 'Bluesky', emoji: '&#129419;', color: 'var(--platform-bsky)', url: 'https://bsky.app/' },
            { key: 'tw', name: 'X / Twitter', emoji: '&#128038;', color: 'var(--platform-tw)', url: 'https://twitter.com/login' },
            { key: 'mast', name: 'Mastodon', emoji: '&#128024;', color: 'var(--platform-mast)', url: 'https://joinmastodon.org/servers' },
            { key: 'tum', name: 'Tumblr', emoji: '&#128216;', color: 'var(--platform-tum)', url: 'https://www.tumblr.com/oauth/apps' },
            { key: 'pix', name: 'Pixiv', emoji: '&#128396;', color: 'var(--platform-pix)', url: 'https://www.pixiv.net/' },
            { key: 'thr', name: 'Threads', emoji: '&#129525;', color: 'var(--platform-thr)', url: 'https://www.threads.net/' },
            { key: 'ig', name: 'Instagram', emoji: '&#128248;', color: 'var(--platform-ig)', url: 'https://www.instagram.com/' },
            { key: 'e621', name: 'e621', emoji: '&#128062;', color: 'var(--platform-e621)', url: 'https://e621.net/session/new' },
        ];

        /* Detect runtime and pre-load existing state so the wizard can
         * pre-populate fields and show "already connected" badges. */
        let runtimeMode = 'desktop';
        let archivePath = '';
        try {
            const status = await API.getSetupStatus().catch(() => ({}));
            runtimeMode = status.runtime_mode || 'desktop';
        } catch { /* ignore */ }
        try {
            const posting = await API.getPostingSettings().catch(() => ({}));
            archivePath = posting.posting_story_archive_path || '';
        } catch { /* ignore */ }

        /* Per-platform connection status (used in the platforms step) */
        const authStatus = {};
        try {
            const [ib, fa, ws, sf, sqw, ao3, da, wp, ik, bsky, tw, mast, tum, pix, thr, ig, e621] = await Promise.all([
                API.getAuthStatus().catch(() => ({})),
                API.getFAAuthStatus().catch(() => ({})),
                API.getWSAuthStatus().catch(() => ({})),
                API.getSFAuthStatus().catch(() => ({})),
                API.getSQWAuthStatus().catch(() => ({})),
                API.getAO3AuthStatus().catch(() => ({})),
                API.getDAAuthStatus().catch(() => ({})),
                API.getWPAuthStatus().catch(() => ({})),
                API.getIKAuthStatus().catch(() => ({})),
                API.getBSKYAuthStatus().catch(() => ({})),
                API.getTWAuthStatus().catch(() => ({})),
                API.getMASTAuthStatus().catch(() => ({})),
                API.getTUMAuthStatus().catch(() => ({})),
                API.getPIXAuthStatus().catch(() => ({})),
                API.getTHRAuthStatus().catch(() => ({})),
                API.getIGAuthStatus().catch(() => ({})),
                API.getE621AuthStatus().catch(() => ({})),
            ]);
            authStatus.ib = ib.has_credentials;
            authStatus.fa = fa.has_cookies;
            authStatus.ws = ws.has_key;
            authStatus.sf = sf.has_credentials;
            authStatus.sqw = sqw.has_credentials;
            authStatus.ao3 = ao3.has_credentials;
            authStatus.da = da.has_credentials;
            authStatus.wp = wp.has_credentials;
            authStatus.ik = ik.has_credentials;
            authStatus.bsky = bsky.has_credentials;
            authStatus.tw = tw.has_credentials;
            authStatus.mast = mast.has_credentials;
            authStatus.tum = tum.has_credentials;
            authStatus.pix = pix.has_credentials;
            authStatus.thr = thr.has_credentials;
            authStatus.ig = ig.has_credentials;
            authStatus.e621 = e621.has_credentials;
        } catch { /* ignore — all default to undefined/false */ }

        /* Wizard state */
        //
        // Restored across a round trip to Settings. The platform step's Connect
        // buttons deep-link into Settings -> Platforms, and renderSetupWizard()
        // re-runs on the way back, so without this the user lands on "Welcome"
        // and re-walks the whole wizard for EVERY platform they connect. With
        // seventeen platforms on that step, that is the difference between a
        // detour and a punishment.
        //
        // sessionStorage: lives exactly as long as this browser tab, so it can
        // never outlive the setup run it belongs to. Wrapped because storage
        // throws outright in some private-browsing modes.
        const _WIZ_KEY = 'pp.setup.progress';
        let _wizSaved = {};
        try { _wizSaved = JSON.parse(sessionStorage.getItem(_WIZ_KEY) || '{}') || {}; } catch (e) { _wizSaved = {}; }
        const _wizSave = () => {
            try { sessionStorage.setItem(_WIZ_KEY, JSON.stringify({ step: currentStep, mode: selectedMode })); }
            catch (e) { /* storage unavailable — the wizard still works, it just forgets */ }
        };

        let currentStep = 'welcome';
        // Server runtime skips mode + pairing entirely — it's always 'server'.
        let selectedMode = runtimeMode === 'server' ? 'server' : null;
        // Mode has to come back BEFORE stepOrder() is consulted — it decides
        // which steps exist, so a restored step is only meaningful alongside it.
        if (runtimeMode !== 'server' && typeof _wizSaved.mode === 'string') selectedMode = _wizSaved.mode;
        let pairingUrl = '';
        let pairingKey = '';
        let pairingError = '';
        let pairingBusy = false;

        /* Step ordering — recomputed each render so paired_desktop's
         * "skip archive + platforms" branch falls out naturally. */
        const stepOrder = () => {
            if (runtimeMode === 'server') {
                return ['welcome', 'archive', 'platforms', 'persona', 'done'];
            }
            if (selectedMode === 'paired_desktop') {
                // Paired installs read the server's data — personas live there.
                return ['welcome', 'mode', 'pairing', 'done'];
            }
            // standalone (or undecided) — full flow
            return ['welcome', 'mode', 'archive', 'platforms', 'persona', 'done'];
        };

        // Validate against the CURRENT path rather than trusting what was stored:
        // the mode picker changes which steps exist, so a step saved under one
        // mode may not exist under another. 'done' is excluded deliberately —
        // returning straight to the final screen would skip the setup it is
        // claiming to have finished.
        if (_wizSaved.step && _wizSaved.step !== 'done' && stepOrder().includes(_wizSaved.step)) {
            currentStep = _wizSaved.step;
        }

        const stepIndex = () => {
            const order = stepOrder();
            const i = order.indexOf(currentStep);
            return i === -1 ? 0 : i;
        };

        const renderStep = () => {
            _wizSave();
            const order = stepOrder();
            const total = order.length;
            const currentIdx = stepIndex();

            // Step indicator dots — one per step in the current path.
            const dots = [];
            for (let i = 0; i < total; i++) {
                const cls = i < currentIdx ? 'active done'
                    : i === currentIdx ? 'active' : '';
                dots.push(`<div class="setup-step-dot ${cls}">${i + 1}</div>`);
                if (i < total - 1) {
                    const lineCls = i < currentIdx ? 'active' : '';
                    dots.push(`<div class="setup-step-line ${lineCls}"></div>`);
                }
            }
            const stepsHtml = `<div class="setup-steps">${dots.join('')}</div>`;

            let body = '';

            if (currentStep === 'welcome') {
                body = `
                    <h1 style="font-size:26px;font-weight:700;color:var(--accent);margin-bottom:8px">Welcome to PawPoller</h1>
                    <p style="color:var(--text-secondary);margin-bottom:8px;font-size:15px">Multi-platform story analytics for furry fiction writers.</p>
                    <p style="color:var(--text-muted);margin-bottom:28px;font-size:13px">Let's get you set up in a few quick steps.</p>
                    <button class="btn btn-primary login-btn" id="setup-next">Get Started</button>`;
            } else if (currentStep === 'mode') {
                /* ── Step 2: How are you running PawPoller? ─────────── */
                body = `
                    <h2 style="font-size:20px;font-weight:700;color:var(--text-primary);margin-bottom:8px">How are you running PawPoller?</h2>
                    <p style="color:var(--text-secondary);margin-bottom:20px;font-size:13px">Pick the option that matches your setup. You can change this later in Settings.</p>
                    <div class="setup-mode-cards">
                        <button class="setup-mode-card ${selectedMode === 'standalone' ? 'selected' : ''}" data-mode="standalone">
                            <div class="setup-mode-emoji">&#128187;</div>
                            <div class="setup-mode-title">Just on this computer</div>
                            <div class="setup-mode-desc">PawPoller polls and posts from your laptop. Nothing else needed.</div>
                        </button>
                        <button class="setup-mode-card ${selectedMode === 'paired_desktop' ? 'selected' : ''}" data-mode="paired_desktop">
                            <div class="setup-mode-emoji">&#9729;&#65039;</div>
                            <div class="setup-mode-title">Pair with my server</div>
                            <div class="setup-mode-desc">I already have a Docker container running. This app reads its settings.</div>
                        </button>
                    </div>
                    <div style="display:flex;gap:8px;margin-top:20px">
                        <button class="btn" id="setup-back" style="flex:0 0 auto;background:transparent;color:var(--text-muted);border:1px solid var(--border)">Back</button>
                        <button class="btn btn-primary login-btn" id="setup-next" style="flex:1" ${selectedMode ? '' : 'disabled'}>Next</button>
                    </div>`;
            } else if (currentStep === 'pairing') {
                /* ── Step 2b: Pairing credentials ──────────────────── */
                const errBlock = pairingError
                    ? `<div class="login-error" style="margin-top:8px">${Utils.escapeHtml(pairingError)}</div>`
                    : '';
                body = `
                    <h2 style="font-size:20px;font-weight:700;color:var(--text-primary);margin-bottom:8px">Connect to your server</h2>
                    <p style="color:var(--text-secondary);margin-bottom:16px;font-size:13px">Enter your PawPoller server URL and an API key. Find or create the key under <em>Settings &rarr; Authentication</em> on the server.</p>
                    <div class="login-field">
                        <label>Server URL</label>
                        <input type="text" id="setup-pair-url" class="search-input" value="${Utils.escapeHtml(pairingUrl)}" placeholder="https://pawpoller.example.com" style="width:100%">
                    </div>
                    <div class="login-field">
                        <label>API key</label>
                        <input type="text" id="setup-pair-key" class="search-input" value="${Utils.escapeHtml(pairingKey)}" placeholder="pp_..." style="width:100%">
                    </div>
                    ${errBlock}
                    <div style="display:flex;gap:8px;margin-top:16px">
                        <button class="btn" id="setup-back" style="flex:0 0 auto;background:transparent;color:var(--text-muted);border:1px solid var(--border)" ${pairingBusy ? 'disabled' : ''}>Back</button>
                        <button class="btn btn-primary login-btn" id="setup-pair-test" style="flex:1" ${pairingBusy ? 'disabled' : ''}>${pairingBusy ? 'Testing...' : 'Connect'}</button>
                    </div>`;
            } else if (currentStep === 'archive') {
                /* ── Story archive ────────────────────────────────── */
                body = `
                    <h2 style="font-size:20px;font-weight:700;color:var(--text-primary);margin-bottom:8px">Story Archive Location</h2>
                    <p style="color:var(--text-secondary);margin-bottom:20px;font-size:13px">Where do your stories live? PawPoller will look here for stories to publish.</p>
                    <div class="login-field">
                        <label>Archive path</label>
                        <input type="text" id="setup-archive-path" class="search-input" value="${Utils.escapeHtml(archivePath)}" placeholder="e.g. C:\\Stories or /home/user/stories" style="width:100%">
                        <div style="font-size:11px;color:var(--text-muted);margin-top:4px">Each story gets its own folder with Markdown, HTML, BBCode, and PDF files.</div>
                    </div>
                    <div style="display:flex;gap:8px;margin-top:8px">
                        ${currentIdx > 0 ? '<button class="btn" id="setup-back" style="flex:0 0 auto;background:transparent;color:var(--text-muted);border:1px solid var(--border)">Back</button>' : ''}
                        <button class="btn btn-primary login-btn" id="setup-next" style="flex:1">Next</button>
                        <button class="btn" id="setup-skip" style="flex:0 0 auto;background:transparent;color:var(--text-muted);border:1px solid var(--border)">Skip</button>
                    </div>`;
            } else if (currentStep === 'platforms') {
                /* ── Platform connections ─────────────────────────── */
                const platformCards = platforms.map(p => {
                    const connected = authStatus[p.key];
                    return `
                        <div class="setup-platform-card ${connected ? 'connected' : ''}">
                            <span class="setup-platform-emoji" style="border-color:${p.color}">${p.emoji}</span>
                            <span class="setup-platform-name">${p.name}</span>
                            <span class="setup-platform-status">${connected ? 'Connected' : 'Not connected'}</span>
                            <a href="#/settings/platforms/${p.key}" class="btn btn-sm setup-platform-connect" style="font-size:11px;padding:4px 10px;margin-top:4px;background:${connected ? 'var(--bg-hover)' : 'var(--accent-dim)'};color:#fff;text-decoration:none;border-radius:var(--radius-sm)">${connected ? 'Edit' : 'Connect'}</a>
                            <a href="${p.url}" target="_blank" rel="noopener" class="setup-platform-site" title="Open ${p.name} in your browser — some platforms need you to generate an API key there first">Open site &#8599;</a>
                        </div>`;
                }).join('');

                body = `
                    <h2 style="font-size:20px;font-weight:700;color:var(--text-primary);margin-bottom:8px">Connect Your Platforms</h2>
                    <p style="color:var(--text-secondary);margin-bottom:16px;font-size:13px">Connect the platforms you publish on. You can skip any and add them later in Settings.</p>
                    <div class="setup-platforms">${platformCards}</div>
                    <div style="display:flex;gap:8px;margin-top:16px">
                        <button class="btn" id="setup-back" style="flex:0 0 auto;background:transparent;color:var(--text-muted);border:1px solid var(--border)">Back</button>
                        <button class="btn btn-primary login-btn" id="setup-next" style="flex:1">Next</button>
                        <button class="btn" id="setup-skip" style="flex:0 0 auto;background:transparent;color:var(--text-muted);border:1px solid var(--border)">Skip for now</button>
                    </div>`;
            } else if (currentStep === 'persona') {
                /* ── Your persona (gap G2) — name the creative identity that
                   groups your accounts + analytics. Skippable; more can be
                   added on the Accounts page any time. ────────────────── */
                body = `
                    <h2 style="font-size:20px;font-weight:700;color:var(--text-primary);margin-bottom:8px">Your persona</h2>
                    <p style="color:var(--text-secondary);margin-bottom:16px;font-size:13px">A persona is the creative identity your accounts belong to — your fursona or pen name. It groups your accounts and pools their analytics across platforms. You can add more later on the Accounts page.</p>
                    <div class="login-field">
                        <label>Persona name</label>
                        <input type="text" id="setup-persona-name" class="search-input" placeholder="e.g. your fursona or pen name" style="width:100%">
                    </div>
                    <div id="setup-persona-msg" style="font-size:12px;margin:6px 0 0;color:var(--danger)"></div>
                    <div style="display:flex;gap:8px;margin-top:16px">
                        <button class="btn" id="setup-back" style="flex:0 0 auto;background:transparent;color:var(--text-muted);border:1px solid var(--border)">Back</button>
                        <button class="btn btn-primary login-btn" id="setup-persona-create" style="flex:1">Create persona</button>
                        <button class="btn" id="setup-skip" style="flex:0 0 auto;background:transparent;color:var(--text-muted);border:1px solid var(--border)">Skip</button>
                    </div>`;
            } else if (currentStep === 'done') {
                const summaryByMode = {
                    'standalone': 'PawPoller is set up to run locally. It\'ll poll and post from this machine.',
                    'paired_desktop': 'Paired with your server. Settings will sync automatically; the server handles polling.',
                    'server': 'Server is ready. Pair a desktop install with the API key generated in Settings.',
                };
                // First-poll offer (gap G2): only when at least one platform is
                // actually connected — polls each connected platform's enabled
                // accounts (the global trigger endpoint is IB-only, so loop).
                const connectedCodes = Object.entries(authStatus).filter(([, v]) => !!v).map(([k]) => k);
                /* Paired installs finish by pulling the server's data (3.18.0).
                   Without this, a fresh pair ends with an app that looks empty
                   and no visible way to fill it — the actual route was an
                   environment variable and a CLI script run from a source
                   checkout, which nothing here would ever have told them. */
                const syncOffer = selectedMode === 'paired_desktop' ? `
                    <button class="btn" id="setup-sync-now" style="margin-bottom:10px;width:100%">&#128260; Bring down my data from the server</button>
                    <div id="setup-sync-msg" style="font-size:12px;color:var(--text-muted);margin-bottom:10px"></div>` : '';
                const firstPoll = connectedCodes.length ? `
                    <button class="btn" id="setup-first-poll" style="margin-bottom:10px;width:100%">&#128202; Run my first poll now (${connectedCodes.length} platform${connectedCodes.length === 1 ? '' : 's'})</button>
                    <div id="setup-first-poll-msg" style="font-size:12px;color:var(--text-muted);margin-bottom:10px"></div>` : '';
                body = `
                    <h2 style="font-size:20px;font-weight:700;color:var(--accent);margin-bottom:8px">You're all set!</h2>
                    <p style="color:var(--text-secondary);margin-bottom:16px;font-size:13px">${Utils.escapeHtml(summaryByMode[selectedMode] || 'PawPoller is ready.')}</p>
                    <ul style="text-align:left;color:var(--text-secondary);font-size:13px;line-height:1.8;margin-bottom:24px;list-style:none;padding:0">
                        <li style="padding:6px 0;border-bottom:1px solid var(--border)">Create a new story in the <strong style="color:var(--text-primary)">Editor</strong></li>
                        <li style="padding:6px 0;border-bottom:1px solid var(--border)">Check your <strong style="color:var(--text-primary)">analytics</strong> on the dashboard</li>
                        <li style="padding:6px 0">Configure more platforms in <strong style="color:var(--text-primary)">Settings</strong></li>
                    </ul>
                    ${firstPoll}
                    ${syncOffer}
                    <button class="btn btn-primary login-btn" id="setup-finish">Go to Dashboard</button>`;
            }

            this._setContent(`
                <div class="login-screen">
                    <div class="login-card setup-wizard">
                        ${stepsHtml}
                        ${body}
                    </div>
                </div>`);

            const goNext = () => {
                const order = stepOrder();
                const i = order.indexOf(currentStep);
                if (i >= 0 && i < order.length - 1) {
                    currentStep = order[i + 1];
                    renderStep();
                }
            };
            const goBack = () => {
                const order = stepOrder();
                const i = order.indexOf(currentStep);
                if (i > 0) {
                    currentStep = order[i - 1];
                    renderStep();
                }
            };

            /* Mode picker — pick a card, click Next */
            document.querySelectorAll('.setup-mode-card').forEach(card => {
                card.addEventListener('click', () => {
                    selectedMode = card.dataset.mode;
                    renderStep();  // re-render to update selection + Next-enabled
                });
            });

            document.getElementById('setup-back')?.addEventListener('click', goBack);

            document.getElementById('setup-next')?.addEventListener('click', async () => {
                if (currentStep === 'mode' && selectedMode === 'standalone') {
                    // Persist standalone mode immediately so the polling
                    // gate kicks in on next restart even if the user
                    // closes the wizard early.
                    try { await API.setSetupMode({ mode: 'standalone' }); }
                    catch (err) { console.warn('[Setup] save standalone failed:', err); }
                }
                if (currentStep === 'archive') {
                    const path = document.getElementById('setup-archive-path')?.value.trim();
                    if (path) {
                        try {
                            await API.savePostingSettings({ posting_story_archive_path: path });
                            archivePath = path;
                        } catch (err) {
                            console.warn('[Setup] Failed to save archive path:', err);
                        }
                    }
                }
                goNext();
            });

            document.getElementById('setup-skip')?.addEventListener('click', goNext);

            /* Persona step (gap G2) — create via the real personas API, then
             * advance. Skip is the plain setup-skip button above. */
            document.getElementById('setup-persona-create')?.addEventListener('click', async () => {
                const nameEl = document.getElementById('setup-persona-name');
                const msgEl = document.getElementById('setup-persona-msg');
                const name = nameEl?.value.trim();
                if (!name) { if (msgEl) msgEl.textContent = 'Give your persona a name (or Skip).'; return; }
                try {
                    await API.createPersona({ name });
                    goNext();
                } catch (err) {
                    if (msgEl) msgEl.textContent = 'Could not create persona: ' + (err.message || err);
                }
            });

            /* First-poll offer on the Done step (gap G2) — fire one poll per
             * connected platform (all its enabled accounts); fire-and-forget,
             * the dashboard fills in as results land. */
            document.getElementById('setup-first-poll')?.addEventListener('click', async () => {
                const btn = document.getElementById('setup-first-poll');
                const msgEl = document.getElementById('setup-first-poll-msg');
                const codes = Object.entries(authStatus).filter(([, v]) => !!v).map(([k]) => k);
                if (!codes.length) return;
                btn.disabled = true;
                btn.textContent = 'Polling…';
                await Promise.allSettled(codes.map(c => API.triggerAccountPoll(c)));
                btn.textContent = 'Poll started ✓';
                if (msgEl) msgEl.textContent =
                    'First poll running — your dashboard will fill in over the next few minutes.';
            });

            /* Pairing test — validate credentials, save mode + creds, advance to 'done' */
            document.getElementById('setup-pair-test')?.addEventListener('click', async () => {
                pairingUrl = document.getElementById('setup-pair-url')?.value.trim() || '';
                pairingKey = document.getElementById('setup-pair-key')?.value.trim() || '';
                if (!pairingUrl || !pairingKey) {
                    pairingError = 'Both fields are required.';
                    renderStep();
                    return;
                }
                pairingError = '';
                pairingBusy = true;
                renderStep();
                try {
                    const result = await API.pairTest({
                        posting_server_url: pairingUrl,
                        posting_server_api_key: pairingKey,
                    });
                    if (!result.ok) {
                        pairingError = result.error || 'Pairing failed.';
                        pairingBusy = false;
                        renderStep();
                        return;
                    }
                    if (result.version_match === false) {
                        pairingError = `Version mismatch: server is ${result.remote_version}, this app is ${result.local_version}. ` +
                                       `Continue anyway, but updating one side may resolve sync issues.`;
                        // Soft warning — keep going.
                    }
                    await API.setSetupMode({
                        mode: 'paired_desktop',
                        posting_server_url: pairingUrl,
                        posting_server_api_key: pairingKey,
                    });
                    pairingBusy = false;
                    goNext();
                } catch (err) {
                    pairingError = err.message || 'Pairing failed.';
                    pairingBusy = false;
                    renderStep();
                }
            });

            document.getElementById('setup-sync-now')?.addEventListener('click', async (ev) => {
                const btn = ev.currentTarget;
                const msg = document.getElementById('setup-sync-msg');
                btn.disabled = true;
                btn.textContent = 'Syncing…';
                try {
                    await API.startMirrorPull({});
                    for (;;) {
                        await new Promise(r => setTimeout(r, 800));
                        let st;
                        try { st = await API.getMirrorPullStatus(); } catch (e) { continue; }
                        if (msg) msg.textContent = st.message || st.phase || 'Working…';
                        if (!st.running) {
                            const res = st.result || {};
                            if (res.error) {
                                if (msg) msg.textContent = res.error;
                                btn.disabled = false;
                                btn.textContent = 'Try again';
                            } else {
                                btn.textContent = 'Synced ✓';
                                // The database applies at startup, so say so
                                // here rather than leaving it to be discovered.
                                if (msg) {
                                    msg.textContent = (res.database || {}).staged
                                        ? 'Done — restart PawPoller to finish applying the database.'
                                        : 'Done.';
                                }
                            }
                            return;
                        }
                    }
                } catch (err) {
                    if (msg) msg.textContent = err.message || String(err);
                    btn.disabled = false;
                    btn.textContent = 'Try again';
                }
            });

            document.getElementById('setup-finish')?.addEventListener('click', async () => {
                const btn = document.getElementById('setup-finish');
                btn.disabled = true;
                btn.textContent = 'Saving...';
                try {
                    await API.markSetupComplete();
                    // The run is over; nothing should be restored into a later one.
                    try { sessionStorage.removeItem(_WIZ_KEY); } catch (e) { /* never block completion */ }
                } catch (err) {
                    console.warn('[Setup] Failed to mark setup complete:', err);
                }
                /* If the user finished the wizard without configuring any
                 * platform, land them on Settings → Platforms instead of
                 * the empty Inkbunny dashboard (BUG-005 in 2.14.6). The
                 * page reload re-runs init() and the normal gates. */
                const hasAnyPlatform = Object.values(authStatus).some(v => !!v);
                window.location.hash = hasAnyPlatform ? '#/' : '#/settings/platforms';
                window.location.reload();
            });
        };

        renderStep();
    },

    /* ── Login Screen ──────────────────────────────────────────
     * renderLogin() — Full-screen login form with username, password, and
     * remember-me checkbox. The submit handler calls API.authLogin, strips
     * "API NNN:" prefixes from error messages, and tries to JSON-parse the
     * detail field for cleaner display. Enter on username focuses password;
     * Enter on password triggers submit. Auto-focuses the username field. */

    renderLogin() {
        if (this._statusCheckInterval) {
            clearInterval(this._statusCheckInterval);
            this._statusCheckInterval = null;
        }
        if (this._progressCheckTimer) {
            clearInterval(this._progressCheckTimer);
            this._progressCheckTimer = null;
        }
        this._setContent(`
            <div class="login-screen">
                <div class="login-card">
                    <h2>PawPoller</h2>
                    <p style="color:var(--text-muted);margin-bottom:20px;font-size:13px">Sign in with your Inkbunny account to get started.</p>
                    <div class="login-field">
                        <label>Username</label>
                        <input type="text" id="login-username" class="search-input" placeholder="Inkbunny username" style="width:100%">
                    </div>
                    <div class="login-field">
                        <label>Password</label>
                        <input type="password" id="login-password" class="search-input" placeholder="Password" style="width:100%">
                    </div>
                    <label class="login-remember">
                        <input type="checkbox" id="login-remember" checked>
                        <span>Remember me</span>
                    </label>
                    <button class="btn btn-primary login-btn" id="login-submit">Sign In</button>
                    <div class="login-error" id="login-error"></div>
                </div>
            </div>
        `);

        const submit = async () => {
            const btn = document.getElementById('login-submit');
            const errEl = document.getElementById('login-error');
            const username = document.getElementById('login-username').value.trim();
            const password = document.getElementById('login-password').value;
            const remember = document.getElementById('login-remember').checked;

            if (!username || !password) {
                errEl.textContent = 'Username and password are required.';
                return;
            }

            btn.disabled = true;
            btn.textContent = 'Signing in...';
            errEl.textContent = '';

            try {
                await API.authLogin({ username, password, remember });
                this.navigate('/loading');
            } catch (err) {
                /* Strip "API 401: " prefix and try to extract JSON .detail for cleaner display */
                let msg = err.message.replace(/^API \d+:\s*/, '');
                try { msg = JSON.parse(msg).detail || msg; } catch {}
                errEl.textContent = msg;
                btn.textContent = 'Sign In';
                btn.disabled = false;
            }
        };

        /* Wire submit button + Enter key handlers: Enter on username focuses password,
         * Enter on password triggers submit */
        document.getElementById('login-submit').addEventListener('click', submit);
        document.getElementById('login-password').addEventListener('keydown', (e) => {
            if (e.key === 'Enter') submit();
        });
        document.getElementById('login-username').addEventListener('keydown', (e) => {
            if (e.key === 'Enter') document.getElementById('login-password').focus();
        });

        // Auto-focus username
        document.getElementById('login-username').focus();
    },

    /* ── Loading Screen ─────────────────────────────────────────
     * renderLoading() — First-run loading screen shown after login when no
     * submission data exists yet. Displays a progress bar and phase label.
     *
     * Flow: triggers an initial poll via API.triggerPoll() (fire-and-forget),
     * then checks /api/poll/progress every 1.5 seconds to update the progress
     * bar percentage and human-readable phase labels (idle -> starting ->
     * logging_in -> searching -> fetching_details -> processing -> complete).
     * During the "processing" phase, progress is interpolated from 40-95%
     * based on current/total submission count. Auto-navigates to the IB
     * dashboard (#/) on completion after a 600 ms delay. On error, displays
     * the error message in red. The poll interval handle is stored in
     * _loadingPollInterval so it can be cleaned up if the user navigates away. */

    renderLoading() {
        this._setContent(`
            <div class="loading-screen">
                <div class="loading-card">
                    <h2>Setting Up</h2>
                    <p id="loading-message" style="color:var(--text-muted);margin-bottom:16px;font-size:13px">Starting initial sync...</p>
                    <div class="progress-bar-container">
                        <div class="progress-bar" id="loading-bar" style="width:0%"></div>
                    </div>
                    <p id="loading-detail" style="color:var(--text-muted);margin-top:10px;font-size:12px"></p>
                    <div id="loading-actions" style="display:none;margin-top:18px;display:flex;gap:8px;justify-content:center;flex-wrap:wrap">
                        <button class="btn btn-secondary" id="loading-skip-overview">Continue to Dashboard</button>
                        <button class="btn btn-secondary" id="loading-skip-settings">Open Settings</button>
                    </div>
                </div>
            </div>
        `);

        // Fire-and-forget: trigger a poll
        API.triggerPoll().catch(() => {});

        const msgEl = document.getElementById('loading-message');
        const barEl = document.getElementById('loading-bar');
        const detailEl = document.getElementById('loading-detail');
        const actionsEl = document.getElementById('loading-actions');

        const showEscapeButtons = () => {
            if (!actionsEl) return;
            actionsEl.style.display = 'flex';
        };

        // Wire escape buttons up front — visible only on error or after the
        // safety timeout below, so the user always has a way out if the
        // initial IB poll fails or stalls (BUG-007 in 2.14.6).
        document.getElementById('loading-skip-overview')?.addEventListener('click', () => {
            this.navigate('/');
        });
        document.getElementById('loading-skip-settings')?.addEventListener('click', () => {
            this.navigate('/settings/platforms');
        });

        // Safety timeout: if the loading screen hasn't completed in 10s,
        // surface the escape buttons. The poll keeps running in the
        // background — the user can navigate away without aborting it.
        const safetyTimer = setTimeout(showEscapeButtons, 10000);

        /* Human-readable labels for each poll phase (shown as progress text) */
        const phaseLabels = {
            idle: 'Waiting to start...',
            starting: 'Initialising...',
            logging_in: 'Authenticating with Inkbunny...',
            searching: 'Searching for submissions...',
            fetching_details: 'Fetching submission details...',
            processing: 'Processing submissions...',
            complete: 'Sync complete!',
            error: 'An error occurred.',
        };

        /* Base progress % for each phase; "processing" interpolates 40-95% from current/total */
        const phaseProgress = {
            idle: 0, starting: 5, logging_in: 10, searching: 20,
            fetching_details: 35, processing: 40, complete: 100, error: 0,
        };

        /* Poll /api/poll/progress every 1.5s; auto-navigate to dashboard on completion */
        if (this._loadingPollInterval) clearInterval(this._loadingPollInterval);
        const pollInterval = setInterval(async () => {
            try {
                const p = await API.getPollProgress();
                const label = phaseLabels[p.phase] || p.message || p.phase;
                if (msgEl) msgEl.textContent = label;

                let pct = phaseProgress[p.phase] || 0;
                if (p.phase === 'processing' && p.total > 0) {
                    pct = 40 + Math.round((p.current / p.total) * 55);
                }
                if (barEl) barEl.style.width = pct + '%';

                if (p.phase === 'processing' && p.total > 0) {
                    if (detailEl) detailEl.textContent = p.current + ' / ' + p.total + ' submissions';
                } else {
                    if (detailEl) detailEl.textContent = p.message || '';
                }

                if (p.phase === 'complete') {
                    clearInterval(pollInterval);
                    clearTimeout(safetyTimer);
                    setTimeout(() => this.navigate('/'), 600);
                }
                if (p.phase === 'error') {
                    clearInterval(pollInterval);
                    clearTimeout(safetyTimer);
                    if (detailEl) {
                        detailEl.textContent = p.message || 'Poll failed. Check Settings for details.';
                        detailEl.style.color = 'var(--danger)';
                    }
                    // Surface escape buttons immediately so the user can
                    // reach Settings to fix credentials (BUG-007).
                    showEscapeButtons();
                }
            } catch {
                // Server might not be ready yet, keep trying
            }
        }, 1500);

        // Store so we can clean up if user navigates away
        this._loadingPollInterval = pollInterval;
    },

    /* ── Combined Overview ───────────────────────────────────────
     * renderOverview() — Cross-platform overview page aggregating data from
     * all four platforms (IB, FA, WS, SF).
     *
     * Fetches IB+FA+WS+SF summaries, aggregate snapshots, top fans, and trending
     * data in parallel via Promise.all, with individual .catch() fallbacks so
     * one platform's failure does not block the others. Merges top-viewed and
     * top-faved lists across platforms by sorting combined arrays by the
     * relevant metric and taking the top 10. Also merges recent faves and
     * comments into a unified recent-activity timeline sorted by first_seen_at.
     *
     * Renders: cross-platform stat totals, per-platform summary cards, trending
     * section (if any), date-range-filtered aggregate charts (one per platform),
     * top viewed/faved lists, recent activity feed, and top fans table.
     * Binds date range bar and starts auto-refresh. */

    /* renderPlatformsHub() — the Platforms hub (#/platforms): a bold
     * colour-tile grid of all 11 platforms with headline stats and a live
     * status dot (populated by platform_health via #pg-status-{code}).
     * Replaces the old modal popover; driven by window.PLATFORMS. */
    async renderPlatformsHub() {
        this._loading();
        const plats = window.PLATFORMS || [];
        const fetchers = {
            ib: () => API.getSummary(), fa: () => API.getFASummary(), ws: () => API.getWSSummary(),
            sf: () => API.getSFSummary(), sqw: () => API.getSQWSummary(), ao3: () => API.getAO3Summary(),
            da: () => API.getDASummary(), wp: () => API.getWPSummary(), ik: () => API.getIKSummary(),
            bsky: () => API.getBSKYSummary(), tw: () => API.getTWSummary(),
            mast: () => API.getMASTSummary(), tum: () => API.getTUMSummary(),
            pix: () => API.getPIXSummary(), thr: () => API.getTHRSummary(), ig: () => API.getIGSummary(),
            e621: () => API.getE621Summary(), fn: () => API.getFNSummary(), fbr: () => API.getFBRSummary(),
        };
        const [results, health] = await Promise.all([
            Promise.all(plats.map(p =>
                (fetchers[p.code] ? fetchers[p.code]() : Promise.resolve(null)).catch(() => null)
            )),
            API.getPlatformsHealth().catch(() => ({})),
        ]);

        const fmt = (n) => Utils.formatCompact(n || 0);
        const tiles = plats.map((p, i) => {
            const d = results[i] || {};
            const views = d.total_views || d.total_reads || 0;
            const faves = d.total_favorites || d.total_votes || d.total_likes || 0;
            const subs = d.total_submissions || 0;
            const primary = views > 0 ? views : faves;
            const primaryLabel = views > 0 ? 'views' : 'faves';
            const route = window.platformRoute ? window.platformRoute(p.code) : '#/' + p.code;
            // No credentials for this platform yet → nudge onboarding with a
            // "Setup guide" button (data-guide is handled by the Guides delegate;
            // it preventDefaults so it opens the guide instead of following the tile).
            const configured = !!(health[p.code] && health[p.code].configured);
            const body = configured
                ? `<div class="hub-tile-num">${fmt(primary)}</div>
                   <div class="hub-tile-sub">${primaryLabel} · ${subs} works</div>`
                : `<div class="hub-tile-sub hub-tile-notset">Not set up yet</div>
                   ${window.PlatformGuides && window.PlatformGuides.has(p.code)
                       ? `<span class="hub-tile-guide" role="button" tabindex="0" data-guide="${p.code}">&#128214; Setup guide</span>`
                       : ''}`;
            return `
                <a href="${route}" class="hub-tile${configured ? '' : ' hub-tile--unset'}" data-platform="${p.code}" style="--pc:${p.color}">
                    <span class="hub-tile-wm">${p.emoji}</span>
                    ${p.pollOnly ? '<span class="hub-tile-pill">poll only</span>' : ''}
                    <div class="hub-tile-top">
                        <span class="hub-tile-logo">${p.logo ? `<img src="${p.logo}" alt="${p.label} logo" loading="lazy">` : `<span class="hub-tile-emoji">${p.emoji}</span>`}</span>
                        <span class="platform-grid-status pp-health-dot" id="pg-status-${p.code}" data-tooltip=""></span>
                    </div>
                    <div class="hub-tile-name">${p.label}</div>
                    ${body}
                </a>`;
        }).join('');

        this._setContent(`
            <div class="page-header">
                <h2>Platforms</h2>
            </div>
            <div class="hub-grid" id="platform-grid">${tiles}</div>
            <p class="logo-disclaimer">Platform names and logos are trademarks of their respective owners.
            PawPoller is an independent tool, not affiliated with or endorsed by any of these platforms;
            their logos are shown solely to identify each service.</p>
        `);

        /* Populate live status dots immediately (platform_health re-fetches
           then renders into #pg-status-{code}). */
        if (window.PlatformHealth && window.PlatformHealth.fetchOnce) {
            window.PlatformHealth.fetchOnce();
        }
    },

    async renderOverview() {
        this._loading();
        try {
            /* Fetch every platform's summary + aggregate in parallel, driven by
             * window.PLATFORMS; .catch() fallbacks prevent one failure blocking
             * the page. Inkbunny is the original platform so its endpoints are
             * unprefixed (getSummary/getAggregate); every other code follows
             * get{CODE}Summary / get{CODE}Aggregate.
             *
             * This replaced a hand-written list of 34 destructured promises
             * that had to be extended in four more places to add a platform —
             * which is why FurryNetwork and Furbooru (2.200/2.201) appeared
             * nowhere on the Overview. Driving it off the registry means a new
             * platform lands here the moment it has a registry entry, and
             * `callOrNull` keeps it safe if the API method doesn't exist yet. */
            const _range = Utils.getDateRange(this._dateRange);
            const codes = window.PLATFORMS.map(p => p.code);
            const apiName = (code, kind) =>
                (code === 'ib' ? 'get' + kind : 'get' + code.toUpperCase() + kind);
            const callOrNull = (name, arg) => (typeof API[name] === 'function'
                ? API[name](arg).catch(() => null)
                : Promise.resolve(null));

            const [summaries, aggregates, topFans, trending] = await Promise.all([
                Promise.all(codes.map(c => callOrNull(apiName(c, 'Summary')))),
                Promise.all(codes.map(c => callOrNull(apiName(c, 'Aggregate'), _range))),
                API.getTopFans(10).catch(() => ({ fans: [] })),
                API.getTrending({ hours: 24, threshold: 2.0 }).catch(() => ({ trending: [] })),
            ]);
            // System events feed + personas (for the "By persona" multi-account
            // widget) — fetched together, after the platform bulk, so a slow
            // /api/activity/recent doesn't block the rest of the page render.
            // Failures fall back to empty. getPersonas already returns each
            // persona with pooled stats.combined, so no per-persona round-trips.
            const [systemActivity, personasResp, queueResp] = await Promise.all([
                API.getRecentActivity(20).catch(() => ({ events: [] })),
                API.getPersonas().catch(() => ({ personas: [] })),
                // Feeds the opt-in "Pending queue" widget; additive, never blocks.
                API.getPostingQueue().catch(() => ({ queue: [] })),
            ]);

            /* code -> payload. Every widget reads these through
             * window.platformStat, so no widget needs to know that Wattpad
             * calls views "reads" or that Tumblr's only metric is "notes". */
            const statsByCode = {};
            const aggByCode = {};
            codes.forEach((c, i) => {
                statsByCode[c] = summaries[i] || {};
                aggByCode[c] = aggregates[i] || null;
            });

            /* Merge a top-list across platforms: tag each row with _platform,
             * normalise the platform's own metric names onto the unified keys
             * the card renderer reads (Wattpad reads/votes, Itaku+Bluesky+
             * Mastodon+Threads+Instagram+Twitter likes, Tumblr notes), sort
             * desc. Kept UNSLICED so a widget filtered to a few platforms still
             * has ten rows to show — the slice happens per widget at render.
             *
             * `pick` receives the platform's summary payload and returns its
             * list; platforms with no such list simply contribute nothing. */
            const mergeTop = (pick, key) => {
                const merged = [];
                window.PLATFORMS.forEach(p => {
                    const s = statsByCode[p.code] || {};
                    // A views list is meaningless for a platform with no view
                    // counter (Itaku/Bluesky/Mastodon/Tumblr/e621/Furbooru).
                    if (key === 'views' && !p.metrics.views) return;
                    (pick(s) || []).forEach(item => merged.push({
                        ...item,
                        views: item.views != null ? item.views : (item.reads || 0),
                        favorites_count: item.favorites_count != null ? item.favorites_count
                            : (item.likes != null ? item.likes
                                : (item.notes != null ? item.notes : (item.votes || 0))),
                        _platform: p.code,
                    }));
                });
                merged.sort((a, b) => (b[key] || 0) - (a[key] || 0));
                return merged;
            };

            const topViewed = mergeTop(s => s.top_viewed || s.top_read, 'views');
            const topFaved = mergeTop(
                s => s.top_faved || s.top_liked || s.top_voted || s.top_noted,
                'favorites_count');

            /* Merge recent faves + comments into a unified timeline, newest
             * first. Each platform names its fave feed after its own metric
             * (recent_likes / recent_notes / recent_votes / recent_bookmarks),
             * so try them in order and take whichever exists. Left unsliced —
             * the widget slices after applying its platform filter. */
            const recentActivity = [];
            window.PLATFORMS.forEach(p => {
                const s = statsByCode[p.code] || {};
                const faves = s.recent_faves || s.recent_likes || s.recent_notes
                    || s.recent_votes || s.recent_bookmarks || [];
                faves.forEach(item => recentActivity.push(
                    { ...item, _platform: p.code, _type: 'fave' }));
                (s.recent_comments || []).forEach(item => recentActivity.push(
                    { ...item, _platform: p.code, _type: 'comment' }));
            });
            recentActivity.sort((a, b) => new Date(b.first_seen_at || 0) - new Date(a.first_seen_at || 0));

            const prefs = await API.getPreferences().catch(() => ({}));

            /* Compact per-platform roll-up. Every widget that shows per-platform
             * numbers (Platform breakdown, Platforms live, Best platform) reads
             * this one array and filters it by its own platform exclusions.
             *
             * `views` is 0 for platforms with no view counter (Itaku, Bluesky,
             * Mastodon, Tumblr) and for the booru family, whose metric is a net
             * score — so none of them can win "best by views". `score` is
             * carried separately rather than being folded into views. */
            const platRollup = window.PLATFORMS.map(p => ({
                code: p.code,
                label: p.label,
                badge: p.code.toUpperCase(),
                subs: window.platformStat(p.code, statsByCode[p.code], 'subs'),
                views: window.platformStat(p.code, statsByCode[p.code], 'views'),
                score: window.platformStat(p.code, statsByCode[p.code], 'score'),
                faves: window.platformStat(p.code, statsByCode[p.code], 'faves'),
                comments: window.platformStat(p.code, statsByCode[p.code], 'comments'),
                // DeviantArt is the only platform reporting downloads today;
                // reading it generically means the next one needs no change.
                downloads: (statsByCode[p.code] || {}).total_downloads || 0,
                // The platform's own word for its headline metric, so the card
                // says "Hits" on AO3 and "Score" on e621 rather than "views".
                primaryLabel: window.platformMetricLabel(
                    p.code, p.metrics.views ? 'views' : (p.metrics.score ? 'score' : 'faves')),
                primary: p.metrics.views
                    ? window.platformStat(p.code, statsByCode[p.code], 'views')
                    : (p.metrics.score
                        ? window.platformStat(p.code, statsByCode[p.code], 'score')
                        : window.platformStat(p.code, statsByCode[p.code], 'faves')),
                favesLabel: window.platformMetricLabel(p.code, 'faves'),
            }));

            /* Per-platform trend charts — only those with history. Each plots
             * that platform's primary snapshot column (`metrics.snap`) and is
             * titled in the platform's own vocabulary, so AO3 reads "AO3 Hits"
             * and Tumblr "Tumblr Notes" rather than everything claiming views.
             * Built from the registry, so FurryNetwork/Furbooru chart too. */
            const chartSpecs = window.PLATFORMS.map(p => ({
                id: `chart-${p.code}-${p.metrics.snap}`,
                code: p.code,
                title: `${p.label} ${window.platformMetricLabel(p.code, p.metrics.snapKey)}`,
                snapshots: aggByCode[p.code] && aggByCode[p.code].snapshots,
                keys: [p.metrics.snap],
            })).filter(c => c.snapshots && c.snapshots.length > 0);

            /* Resolve the server-saved widget layout (cross-device); validate
               ids/spans against the catalog and fall back to the default. */
            const validIds = new Set(this._dashWidgetMeta().map(m => m.id));
            const saved = Array.isArray(prefs.dashboard_layout) ? prefs.dashboard_layout : null;
            // Multi-account = 2+ personas, or 2+ accounts pooled across personas.
            const personaList = (personasResp.personas || []).filter(p => p && p.persona_id);
            const totalAccounts = personaList.reduce((n, p) => n + ((p.accounts || []).length), 0);
            const multiAccount = personaList.length >= 2 || totalAccounts >= 2;
            const defaultLayout = this._dashDefaultLayout(multiAccount);
            let layout = (saved && saved.length ? saved : defaultLayout)
                .filter(w => w && validIds.has(w.id))
                .map(w => {
                    const e = { id: w.id, span: [1, 2, 4].includes(w.span) ? w.span : 1 };
                    // Preserve per-widget config (e.g. the charts widget's chartType).
                    if (w.cfg && typeof w.cfg === 'object') e.cfg = w.cfg;
                    return e;
                });
            if (!layout.length) layout = defaultLayout;
            this._dashboardLayout = layout;
            if (this._dashEdit === undefined) this._dashEdit = false;

            /* Cache everything the widgets need so customise-mode edits (and
               per-widget platform filters) re-render instantly without
               re-fetching.

               Note what is NOT pre-computed any more: totals, the platform
               breakdown HTML and the chart HTML used to be baked once here.
               They are now derived per widget in _dashWidgetHtml, because each
               widget can exclude a different set of platforms — so the numbers
               depend on which widget is asking. The unfiltered source data
               (platRollup, charts, topViewed/topFaved, recentActivity) is
               cached instead, unsliced. */
            this._dashCtx = {
                charts: chartSpecs,
                topViewed, topFaved,
                recentActivity,
                topFans: (topFans.fans || []).slice(0, 10),
                trending: (trending.trending || []),
                systemActivity: (systemActivity.events || []).slice(0, 20),
                personas: (personasResp.personas || []),
                platRollup,
                queue: (queueResp && queueResp.queue) || [],
            };

            this._renderDashboard();
            this._startAutoRefresh(() => { if (!this._dashEdit) this.renderOverview(); });
        } catch (err) {
            this._setContent(`<div class="empty-state"><h3>Error loading overview</h3><p>${Utils.escapeHtml(err.message)}</p></div>`);
        }
    },

    /* ═══ Configurable Home dashboard ═══════════════════════════
     * The Home/Overview is a widget grid the user can customise. Layout
     * (a list of {id, span}) is server-saved via the dashboard_layout
     * preference so it follows them across desktop + phone. renderOverview()
     * fetches + caches the data (this._dashCtx); these helpers render the
     * grid and handle customise-mode (add/remove/resize/drag) cheaply from
     * the cache without re-fetching. */

    _dashDefaultLayout(includePersonas = false) {
        const layout = [
            { id: 'health', span: 4 },
            { id: 'stat-subs', span: 1 }, { id: 'stat-views', span: 1 },
            { id: 'stat-faves', span: 1 }, { id: 'stat-comments', span: 1 },
            { id: 'charts', span: 4 }, { id: 'platforms', span: 4 },
        ];
        // Multi-account users get the "By persona" roll-up up top by default, so
        // the multi-account Overview is there without opening ⚙ Customize.
        if (includePersonas) layout.push({ id: 'personas', span: 4 });
        layout.push(
            { id: 'topviewed', span: 2 }, { id: 'topfaved', span: 2 },
            { id: 'activity', span: 2 }, { id: 'topfans', span: 2 },
        );
        return layout;
    },

    _dashWidgetMeta() {
        return [
            { id: 'health', title: 'Platform health', icon: '\u{1FA7A}', desc: 'Live status of all 16 platforms', spans: [4] },
            { id: 'stat-subs', title: 'Submissions', icon: '\u{1F4E6}', desc: 'Total works tracked', spans: [1, 2] },
            { id: 'stat-views', title: 'Total views', icon: '\u{1F441}', desc: 'Aggregate views', spans: [1, 2] },
            { id: 'stat-faves', title: 'Favourites', icon: '★', desc: 'Aggregate favourites', spans: [1, 2] },
            { id: 'stat-comments', title: 'Comments', icon: '\u{1F4AC}', desc: 'Aggregate comments', spans: [1, 2] },
            { id: 'stat-downloads', title: 'Downloads', icon: '⬇', desc: 'Aggregate downloads', spans: [1, 2] },
            { id: 'charts', title: 'Views over time', icon: '\u{1F4C8}', desc: 'Per-platform trend charts', spans: [2, 4] },
            { id: 'platforms', title: 'Platform breakdown', icon: '\u{1F43E}', desc: 'Per-platform stat cards', spans: [2, 4] },
            { id: 'trending', title: 'Trending now', icon: '\u{1F525}', desc: 'Fast-rising works', spans: [2, 4] },
            { id: 'topviewed', title: 'Top viewed', icon: '\u{1F3C6}', desc: 'Most-viewed works', spans: [2, 4] },
            { id: 'topfaved', title: 'Top faved', icon: '❤', desc: 'Most-favourited works', spans: [2, 4] },
            { id: 'activity', title: 'Recent activity', icon: '\u{1F4AC}', desc: 'Latest faves & comments', spans: [2, 4] },
            { id: 'topfans', title: 'Top fans', icon: '\u{1F451}', desc: 'Most engaged readers', spans: [2, 4] },
            { id: 'events', title: 'System events', icon: '\u{1F6CE}', desc: 'Polls, posts and alerts', spans: [2, 4] },
            { id: 'personas', title: 'By persona', icon: '\u{1F465}', desc: 'Pooled stats per persona (multi-account)', spans: [2, 4] },
            { id: 'quicklinks', title: 'Quick actions', icon: '⚡', desc: 'Jump to create a story, artwork or post', spans: [2, 4] },
            { id: 'engagement', title: 'Engagement rate', icon: '\u{1F4CA}', desc: 'Interactions per view + average views per work', spans: [1, 2] },
            { id: 'milestones', title: 'Milestones', icon: '\u{1F3AF}', desc: 'Progress to your next round-number goal', spans: [2, 4] },
            { id: 'spotlight', title: 'Spotlight', icon: '⭐', desc: 'Your single best-performing work', spans: [2, 4] },
            { id: 'stat-platforms', title: 'Platforms live', icon: '\u{1F310}', desc: 'How many platforms you have work on', spans: [1, 2] },
            { id: 'bestplatform', title: 'Best platform', icon: '\u{1F947}', desc: 'Where your work gets the most views', spans: [1, 2] },
            { id: 'comments', title: 'Recent comments', icon: '\u{1F5E8}', desc: 'Just the comments — people talking to you', spans: [2, 4] },
            { id: 'queue', title: 'Pending queue', icon: '⏳', desc: 'Uploads waiting to go out', spans: [2, 4] },
        ];
    },

    /* ── Per-widget platform filter ────────────────────────────────────────
     * Any widget with a platform dimension can be narrowed to a subset of
     * platforms from ⚙ Customize. The choice rides on the layout entry's `cfg`
     * (the same channel the charts widget uses for its line/bar toggle), so it
     * saves to the server with the layout and follows the user across devices.
     *
     * cfg.exclude stores the codes switched OFF, never the ones switched ON.
     * That is the whole design decision: a platform connected later shows up in
     * every widget by default, so a board configured months ago cannot quietly
     * under-count new work. The cost is that a widget set to "just FA and IB"
     * will gain a third platform when one is connected — the safer failure. */
    _widgetExclude(w) {
        return (w && w.cfg && Array.isArray(w.cfg.exclude)) ? w.cfg.exclude : [];
    },

    /* Widgets that honour the filter. Feed-style widgets with no platform
     * dimension (Top fans, System events, By persona, Quick actions, Pending
     * queue, Platform health) are deliberately absent. */
    _PLATFORM_FILTERABLE: [
        'stat-subs', 'stat-views', 'stat-faves', 'stat-comments', 'stat-downloads',
        'stat-platforms', 'engagement', 'milestones', 'spotlight', 'bestplatform',
        'platforms', 'charts', 'trending', 'topviewed', 'topfaved', 'activity', 'comments',
    ],

    /* Aggregate totals over the platforms a widget still includes. Derived per
     * widget rather than once per page, because two stat cards on the same
     * board can be scoped to different platforms. */
    _dashTotals(ctx, exclude) {
        const rows = (ctx.platRollup || [])
            .filter(p => window.isPlatformVisible(p.code, exclude));
        const sum = key => rows.reduce((n, p) => n + (p[key] || 0), 0);
        return {
            subs: sum('subs'), views: sum('views'), score: sum('score'),
            faves: sum('faves'), comments: sum('comments'), downloads: sum('downloads'),
        };
    },

    /* "3 of 19 platforms" caption for a filtered widget, so a small number is
     * never mistaken for a bad poll. Empty when nothing is excluded. */
    _filterNote(ctx, exclude) {
        if (!exclude || !exclude.length) return '';
        const total = (ctx.platRollup || []).length;
        return `<span class="wfilter-note" title="This widget is filtered — edit it under ⚙ Customize">`
            + `${total - exclude.length} of ${total} platforms</span>`;
    },

    /* Per-platform mini stat card. Labels follow the platform's own vocabulary
     * (AO3 "Hits", e621 "Score", Tumblr "Notes") instead of calling everything
     * views — the registry carries that wording. */
    _platformCardHtml(p) {
        const cell = (value, label) =>
            `<div><span style="font-size:18px;font-weight:600">${Utils.formatCompact(value || 0)}</span>`
            + ` <span style="font-size:11px;color:var(--text-muted)">${Utils.escapeHtml(label.toLowerCase())}</span></div>`;
        return `
                <a href="#/${p.code}" class="stat-card" style="text-decoration:none;color:inherit;cursor:pointer;transition:box-shadow 0.2s">
                    <div class="label"><span class="platform-badge ${p.code}">${Utils.escapeHtml(p.badge)}</span> ${Utils.escapeHtml(p.label)}</div>
                    <div style="display:flex;gap:16px;margin-top:6px">
                        ${cell(p.primary, p.primaryLabel)}
                        ${cell(p.faves, p.favesLabel)}
                        ${cell(p.subs, 'subs')}
                    </div>
                </a>`;
    },

    _dashWidgetHtml(id, ctx, w) {
        // Stat cards deep-link into the works library (all your submissions —
        // stories + artwork). Not linked while customising the board, so drag /
        // resize keeps a plain, non-navigating cell. Nav goes through the global
        // [data-nav] click delegate.
        const exclude = this._widgetExclude(w);
        const vis = code => window.isPlatformVisible(code, exclude);
        const totals = this._dashTotals(ctx, exclude);
        const note = this._filterNote(ctx, exclude);
        const stat = (label, value, nav) => {
            const inner = `<div class="wtitle">${label}${note}</div><div class="w-num">${Utils.formatCompact(value || 0)}</div>`;
            return (nav && !this._dashEdit)
                ? `<a class="dash-stat-link" data-nav="${nav}" title="View your works">${inner}</a>`
                : inner;
        };
        switch (id) {
            // Each metric card lands on the shelf sorted by THAT metric (2.147.0);
            // Submissions/Downloads have no metric sort, so they open the plain shelf.
            case 'stat-subs': return stat('Submissions', totals.subs, '#/library');
            case 'stat-views': return stat('Total views', totals.views, '#/library/sort/views');
            case 'stat-faves': return stat('Favourites', totals.faves, '#/library/sort/favorites');
            case 'stat-comments': return stat('Comments', totals.comments, '#/library/sort/comments');
            case 'stat-downloads': return stat('Downloads', totals.downloads, '#/library');
            case 'health': return this._healthStripHtml();
            case 'platforms': {
                const cards = (ctx.platRollup || []).filter(p => vis(p.code))
                    .map(p => this._platformCardHtml(p)).join('');
                return `<div class="wtitle">Platform breakdown${note}</div>`
                    + `<div class="dash-platgrid">${cards || '<div class="dash-empty">No platforms selected.</div>'}</div>`;
            }
            case 'charts': {
                const ct = (w && w.cfg && w.cfg.chartType === 'bar') ? 'bar' : 'line';
                const toggle = `<span class="wb-charttype" role="group" aria-label="Chart type">`
                    + `<button type="button" class="wb-ct${ct === 'line' ? ' is-active' : ''}" data-chart-type="line">Line</button>`
                    + `<button type="button" class="wb-ct${ct === 'bar' ? ' is-active' : ''}" data-chart-type="bar">Bar</button></span>`;
                const specs = (ctx.charts || []).filter(c => vis(c.code));
                const html = specs.length
                    ? specs.map(c => `<div class="chart-container"><h3>${Utils.escapeHtml(c.title)}</h3><div class="chart-wrap"><canvas id="${c.id}"></canvas></div></div>`).join('')
                    : '<div class="dash-empty">No view history yet — charts appear after a poll or two.</div>';
                return `<div class="wtitle">Views over time${note}${toggle}</div>${html}`;
            }
            case 'trending': {
                const items = (ctx.trending || []).filter(t => vis(t.platform));
                return `<div class="wtitle">Trending now${note}</div>${items.length ? `<div class="stats-grid" style="margin:0">${Components.trendingCards(items)}</div>` : '<div class="dash-empty">Nothing trending right now.</div>'}`;
            }
            case 'topviewed': return `<div class="wtitle">Top viewed${note}</div>${Components.overviewTopList((ctx.topViewed || []).filter(i => vis(i._platform)).slice(0, 10), 'views')}`;
            case 'topfaved': return `<div class="wtitle">Top faved${note}</div>${Components.overviewTopList((ctx.topFaved || []).filter(i => vis(i._platform)).slice(0, 10), 'favorites_count')}`;
            case 'activity': return `<div class="wtitle">Recent activity${note}</div>${Components.overviewRecentActivity((ctx.recentActivity || []).filter(a => vis(a._platform)).slice(0, 15))}`;
            case 'topfans': return `<div class="wtitle">Top fans</div>${Components.topFansTable(ctx.topFans)}`;
            case 'events': return `<div class="wtitle">System events</div>${Components.systemEventsFeed(ctx.systemActivity)}`;
            case 'personas': return `<div class="wtitle">By persona <span class="muted" style="font-weight:400;font-size:.8rem">— your accounts, pooled by identity</span></div>${this._personasWidgetHtml(ctx.personas)}`;
            case 'quicklinks': return `<div class="wtitle">Quick actions</div>${this._quickLinksHtml()}`;
            case 'engagement': return this._engagementHtml(totals, note);
            case 'milestones': return `<div class="wtitle">Milestones${note}</div>${this._milestonesHtml(totals)}`;
            case 'spotlight': return `<div class="wtitle">Spotlight${note}</div>${this._spotlightHtml((ctx.topViewed || []).filter(i => vis(i._platform)))}`;
            case 'stat-platforms': {
                const shown = (ctx.platRollup || []).filter(p => vis(p.code));
                const live = shown.filter(p => p.subs > 0);
                return `<div class="wtitle">Platforms live${note}</div>`
                    + `<a class="dash-stat-link" data-nav="#/platforms" title="Open the Platforms hub">`
                    + `<div class="w-num">${live.length}<span style="font-size:.45em;color:var(--text-muted)"> / ${shown.length}</span></div>`
                    + `<div class="dash-sub">${live.length ? live.slice(0, 6).map(p => Utils.escapeHtml(p.label)).join(' · ') : 'Nothing posted yet'}</div></a>`;
            }
            case 'bestplatform': return this._bestPlatformHtml((ctx.platRollup || []).filter(p => vis(p.code)), note);
            case 'comments': {
                const only = (ctx.recentActivity || [])
                    .filter(a => a._type === 'comment' && vis(a._platform)).slice(0, 15);
                return `<div class="wtitle">Recent comments${note}</div>`
                    + (only.length ? Components.overviewRecentActivity(only)
                        : '<div class="dash-empty">No comments yet — they’ll show here as they land.</div>');
            }
            case 'queue': return `<div class="wtitle">Pending queue</div>${this._queueWidgetHtml(ctx.queue)}`;
            default: return '<div class="wtitle">Widget</div>';
        }
    },

    /* "By persona" Overview widget — a compact multi-account roll-up. Each row is
     * one persona (its accounts pooled) linking to that persona's detail page.
     * getPersonas() already returns stats.combined per persona, so this is a pure
     * render. Empty state nudges single-account users toward the feature. */
    _personasWidgetHtml(personas) {
        const list = (personas || []).filter(p => p && p.persona_id);
        if (!list.length) {
            return '<div class="dash-empty">No personas yet — group your accounts into personas on the '
                + '<a href="#/accounts">Accounts</a> page to see per-identity stats here.</div>';
        }
        const fmt = n => Utils.formatCompact(n || 0);
        const rows = list.map(p => {
            const c = (p.stats && p.stats.combined) || {};
            const nAcct = (p.accounts || []).length;
            const sw = `<span style="display:inline-block;width:12px;height:12px;border-radius:3px;`
                + `background:${Utils.escapeHtml(p.color || '#6c8cff')};margin-right:.5rem;vertical-align:middle;"></span>`;
            return `<a href="#/persona/${p.persona_id}" style="display:flex;align-items:center;gap:.6rem;`
                + `justify-content:space-between;padding:.5rem .1rem;border-bottom:1px solid var(--border);`
                + `text-decoration:none;color:inherit;flex-wrap:wrap;">`
                + `<span style="font-weight:600;min-width:0;">${sw}${Utils.escapeHtml(p.name || 'Persona')}</span>`
                + `<span class="muted" style="font-size:.82rem;">${nAcct} account${nAcct === 1 ? '' : 's'}`
                + ` · \u{1F441} ${fmt(c.views)} · ❤ ${fmt(c.favorites)} · \u{1F4AC} ${fmt(c.comments)}</span></a>`;
        }).join('');
        return `<div>${rows}</div>`;
    },

    /* "Quick actions" widget — one-tap jumps to the create flows. Uses the
     * global [data-nav] delegate; the editing CSS disables inner pointer events
     * so a drag never fires a nav. */
    _quickLinksHtml() {
        const links = [
            { nav: '#/artwork/quick', icon: '⚡', label: 'Quick publish' },
            { nav: '#/editor', icon: '✍️', label: 'New story' },
            { nav: '#/artwork/new', icon: '\u{1F5BC}️', label: 'New artwork' },
            { nav: '#/posts/new', icon: '\u{1F4AC}', label: 'New post' },
            { nav: '#/library', icon: '\u{1F4DA}', label: 'Library' },
        ];
        return `<div class="dash-quicklinks">` + links.map(l =>
            `<a class="dash-ql" data-nav="${l.nav}"><span class="dash-ql-ico">${l.icon}</span>`
            + `<span>${l.label}</span></a>`).join('') + `</div>`;
    },

    /* "Engagement rate" widget — interactions (faves + comments) per view, plus
     * average views per work. Both derived from the cached totals, no fetch. */
    _engagementHtml(t, note) {
        const views = t.views || 0;
        const interactions = (t.faves || 0) + (t.comments || 0);
        const rate = views ? (interactions / views * 100) : 0;
        const avg = t.subs ? Math.round(views / t.subs) : 0;
        return `<div class="wtitle">Engagement rate${note || ''}</div>`
            + `<div class="w-num">${rate.toFixed(1)}<span style="font-size:.5em">%</span></div>`
            + `<div class="dash-sub">${Utils.formatCompact(avg)} avg views/work · ${Utils.formatCompact(interactions)} interactions</div>`;
    },

    /* Next round-number goal above the current value (for the milestone bars). */
    _nextMilestone(n) {
        n = n || 0;
        const steps = [100, 250, 500, 1000, 2500, 5000, 10000, 25000, 50000,
            100000, 250000, 500000, 1000000, 2500000, 5000000, 10000000];
        for (const s of steps) if (s > n) return s;
        return Math.ceil((n + 1) / 1000000) * 1000000;
    },
    _milestoneRow(label, value) {
        const v = value || 0;
        const goal = this._nextMilestone(v);
        const pct = Math.min(100, Math.round((v / goal) * 100));
        return `<div class="dash-ms">
            <div class="dash-ms-head"><span>${label}</span>`
            + `<span class="muted">${Utils.formatCompact(v)} / ${Utils.formatCompact(goal)}</span></div>
            <div class="dash-ms-bar"><span style="width:${pct}%"></span></div>
        </div>`;
    },
    _milestonesHtml(t) {
        return `<div class="dash-ms-wrap">`
            + this._milestoneRow('Views', t.views)
            + this._milestoneRow('Favourites', t.faves)
            + this._milestoneRow('Comments', t.comments)
            + `</div>`;
    },

    /* "Spotlight" widget — the single best-performing work (top of topViewed). */
    _spotlightHtml(topViewed) {
        const top = (topViewed || [])[0];
        if (!top) return '<div class="dash-empty">No works yet — publish something to see your top performer here.</div>';
        const P = (window.PLATFORMS || []).find(p => p.code === top._platform);
        const emoji = P && P.emoji ? P.emoji : '';
        const label = P ? P.label : (top._platform || '');
        return `<div class="dash-spot">
            <div class="dash-spot-title">${Utils.escapeHtml(top.title || 'Untitled')}</div>
            <div class="dash-spot-plat">${emoji} ${Utils.escapeHtml(label)}</div>
            <div class="dash-spot-stats">
                <span><b>${Utils.formatCompact(top.views || 0)}</b> views</span>
                <span><b>${Utils.formatCompact(top.favorites_count || 0)}</b> faves</span>
            </div>
        </div>`;
    },

    /* "Best platform" — where your work actually gets seen. Ranks by views, so
     * engagement-only platforms (no view counts) never spuriously win. */
    _bestPlatformHtml(rollup, note) {
        const ranked = (rollup || []).filter(p => p.views > 0).sort((a, b) => b.views - a.views);
        if (!ranked.length) return `<div class="wtitle">Best platform${note || ''}</div>`
            + '<div class="dash-empty">No view data yet — it appears after a poll or two.</div>';
        const top = ranked[0];
        const P = (window.PLATFORMS || []).find(p => p.code === top.code);
        const runner = ranked[1];
        return `<div class="wtitle">Best platform${note || ''}</div>`
            + `<a class="dash-stat-link" data-nav="#/${top.code}" title="Open ${Utils.escapeHtml(top.label)}">`
            + `<div class="dash-spot-title">${P && P.emoji ? P.emoji + ' ' : ''}${Utils.escapeHtml(top.label)}</div>`
            + `<div class="dash-spot-stats"><span><b>${Utils.formatCompact(top.views)}</b> views</span>`
            + `<span><b>${Utils.formatCompact(top.subs)}</b> works</span></div>`
            + (runner ? `<div class="dash-sub">Runner-up: ${Utils.escapeHtml(runner.label)} · ${Utils.formatCompact(runner.views)} views</div>` : '')
            + `</a>`;
    },

    /* "Pending queue" — what's still waiting to go out (desktop-required posts,
     * scheduled items, retries). Actionable, so it links to the queue page. */
    _queueWidgetHtml(queue) {
        const items = (queue || []).filter(q => q && q.status !== 'completed');
        if (!items.length) return '<div class="dash-empty">Nothing queued — everything’s out. ✅</div>';
        const rows = items.slice(0, 6).map(q => {
            const P = (window.PLATFORMS || []).find(p => p.code === q.platform);
            const ch = q.chapter_index > 0 ? `Ch${q.chapter_index}` : 'Full';
            return `<div style="display:flex;justify-content:space-between;gap:.6rem;padding:.4rem .1rem;`
                + `border-bottom:1px solid var(--border);font-size:.85rem;">`
                + `<span style="min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">`
                + `${P && P.emoji ? P.emoji + ' ' : ''}${Utils.escapeHtml(q.story_name || '')} <span class="muted">${ch}</span></span>`
                + `<span class="muted">${Utils.escapeHtml(q.status || 'pending')}</span></div>`;
        }).join('');
        const more = items.length > 6 ? `<div class="dash-sub">+${items.length - 6} more</div>` : '';
        return `<a class="dash-stat-link" data-nav="#/posting/queue" title="Open the posting queue">${rows}${more}</a>`;
    },

    _dashWidgetMount(id, ctx, w) {
        if (id === 'charts') {
            const type = (w && w.cfg && w.cfg.chartType === 'bar') ? 'bar' : 'line';
            // Mount only the charts this widget actually rendered — the
            // excluded ones have no canvas in the DOM.
            const exclude = this._widgetExclude(w);
            ctx.charts.filter(c => window.isPlatformVisible(c.code, exclude)).forEach(c => {
                try { Charts.aggregateLine(c.id, c.snapshots, c.keys, type); } catch (e) { /* canvas may be absent */ }
            });
        } else if (id === 'health') {
            this._mountHealthStrip();
        }
    },

    /* ── Platform health strip (Overview widget · "Observatory") ──
     * A compact, live 16-platform status strip. Reads the shared
     * PlatformHealth cache + subscribes for updates (no new fetch of its
     * own — PlatformHealth already polls /api/platforms/health). */
    _healthStripHtml() {
        const PH = window.PlatformHealth;
        const codes = (window.PLATFORMS && window.PLATFORMS.length)
            ? window.PLATFORMS.map(p => p.code)
            : ['ib', 'fa', 'ws', 'sf', 'sqw', 'ao3', 'da', 'wp', 'ik', 'bsky', 'tw', 'mast', 'tum', 'pix', 'thr', 'ig'];
        const label = c => (PH && PH.LABELS && PH.LABELS[c]) || c;
        const emoji = c => { const p = (window.PLATFORMS || []).find(x => x.code === c); return p && p.emoji ? p.emoji : ''; };
        const chips = codes.map(c => {
            const st = PH ? PH.classify(c) : 'unknown';
            return `<span class="hs-chip" data-hs="${c}" title="${Utils.escapeHtml(label(c))}">`
                + `<span class="hs-dot pp-health-${st}"></span>`
                + `<span class="hs-label">${emoji(c)} ${Utils.escapeHtml(label(c))}</span></span>`;
        }).join('');
        return `<div class="wtitle">Platform health <span class="hs-summary" id="hs-summary"></span></div>`
            + `<div class="health-strip" id="health-strip">${chips}</div>`;
    },

    _healthSummaryText(counts) {
        const healthy = counts.healthy || 0;
        const attention = (counts.error || 0) + (counts.stale || 0) + (counts.throttled || 0);
        const off = (counts.unconfigured || 0) + (counts.unknown || 0);
        const parts = [];
        if (healthy) parts.push(`${healthy} healthy`);
        if (attention) parts.push(`${attention} need${attention === 1 ? 's' : ''} attention`);
        if (off) parts.push(`${off} not set up`);
        return parts.join(' · ');
    },

    _mountHealthStrip() {
        const PH = window.PlatformHealth;
        if (!PH) return;
        const paint = () => {
            const strip = document.getElementById('health-strip');
            if (!strip) return true;   // widget gone → signal unsubscribe
            const counts = {};
            strip.querySelectorAll('.hs-chip').forEach(chip => {
                const c = chip.getAttribute('data-hs');
                const st = PH.classify(c);
                counts[st] = (counts[st] || 0) + 1;
                const dot = chip.querySelector('.hs-dot');
                if (dot) dot.className = 'hs-dot pp-health-' + st;
                const entry = PH.get(c);
                const last = entry && entry.last_poll_at ? ` · polled ${PH.relativePast(entry.last_poll_at)}` : '';
                chip.title = `${(PH.LABELS && PH.LABELS[c]) || c}: ${st}${last}`;
            });
            const sum = document.getElementById('hs-summary');
            if (sum) sum.textContent = this._healthSummaryText(counts);
            return false;
        };
        paint();
        // Re-subscribe cleanly on every re-render (avoid stacking subscribers).
        if (this._healthUnsub) { this._healthUnsub(); this._healthUnsub = null; }
        this._healthUnsub = PH.subscribe(() => {
            if (paint() && this._healthUnsub) { this._healthUnsub(); this._healthUnsub = null; }
        });
        if (PH.fetchOnce) PH.fetchOnce();
    },

    /* ── "What's new" popup (shown once when the running version changed) ──
     * The app pops a changelog when the version it's serving differs from the one
     * this browser last saw (a self-update or a server redeploy). Sourced from the
     * bundled CHANGELOG.md via /api/whatsnew (auth-gated → no-ops before login). */
    async _maybeShowWhatsNew() {
        let seen = null;
        try { seen = localStorage.getItem('pp_seen_version'); } catch (e) { return; }
        let data;
        try { data = await API.getWhatsNew(seen || ''); } catch (e) { return; }   // 401 pre-login, etc.
        const current = data && data.current;
        if (!current) return;
        if (!seen) {                                   // first run on this browser → remember silently
            try { localStorage.setItem('pp_seen_version', current); } catch (e) { /* private mode */ }
            return;
        }
        if (current !== seen && data.entries && data.entries.length) {
            this._showWhatsNewModal(data);
        }
        try { localStorage.setItem('pp_seen_version', current); } catch (e) { /* private mode */ }
    },

    _showWhatsNewModal(data) {
        const esc = (s) => Utils.escapeHtml(String(s == null ? '' : s));
        // The SUMMARY only — one plain-language line per release, never the full
        // entry (2.156.0). CHANGELOG.md is the engineering record; popping its
        // internals at someone who just wanted to know what changed is noise.
        const items = (data.entries || []).map(e => `
            <div class="wn-entry">
                <div class="wn-ver">v${esc(e.version)}${e.header ? ` <span class="wn-head">${esc(e.header)}</span>` : ''}</div>
                <div class="wn-body">${this._mdLite(e.summary || '')}</div>
            </div>`).join('');
        const more = data.truncated
            ? '<p class="wn-more">…and earlier updates too — see CHANGELOG.md for the full history.</p>' : '';
        let ov = document.getElementById('whatsnew-ov');
        if (!ov) { ov = document.createElement('div'); ov.id = 'whatsnew-ov'; ov.className = 'wn-ov'; document.body.appendChild(ov); }
        ov.innerHTML = `
            <div class="wn-modal" role="dialog" aria-modal="true" aria-label="What's new">
                <div class="wn-top">
                    <h2>✨ What's new <span class="wn-cur">v${esc(data.current)}</span></h2>
                    <button class="wn-x" type="button" aria-label="Close">×</button>
                </div>
                <div class="wn-scroll">${items}${more}</div>
                <div class="wn-foot"><button class="btn btn-primary wn-got" type="button">Got it</button></div>
            </div>`;
        ov.classList.add('open');
        const close = () => ov.classList.remove('open');
        ov.querySelector('.wn-x').addEventListener('click', close);
        ov.querySelector('.wn-got').addEventListener('click', close);
        ov.addEventListener('click', (e) => { if (e.target === ov) close(); });   // backdrop
    },

    /* Minimal, SAFE markdown for changelog bodies: escape everything first, then
     * apply a tiny allow-list (bold, inline code, bullet lists, ### subheads). No
     * raw HTML from the source ever reaches the DOM. */
    _mdLite(src) {
        const inline = (t) => Utils.escapeHtml(t)
            .replace(/`([^`]+)`/g, '<code>$1</code>')
            .replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');
        let html = '', inList = false;
        for (const raw of String(src || '').split('\n')) {
            const line = raw.replace(/\s+$/, '');
            const li = line.match(/^\s*[-*]\s+(.*)$/);
            if (li) {
                if (!inList) { html += '<ul>'; inList = true; }
                html += `<li>${inline(li[1])}</li>`;
                continue;
            }
            if (inList) { html += '</ul>'; inList = false; }
            if (!line.trim()) continue;                          // blank → block spacing
            if (/^#{1,6}\s/.test(line)) html += `<p class="wn-sub">${inline(line.replace(/^#{1,6}\s/, ''))}</p>`;
            else html += `<p>${inline(line)}</p>`;
        }
        if (inList) html += '</ul>';
        return html;
    },

    /* _renderDashboard() — (re)build the Home widget grid from this._dashCtx +
     * this._dashboardLayout. Called on initial load and on every customise
     * edit (cheap; no re-fetch). */
    _renderDashboard() {
        const ctx = this._dashCtx;
        if (!ctx) return;
        const edit = this._dashEdit;
        const layout = this._dashboardLayout || this._dashDefaultLayout();
        const metaById = {};
        this._dashWidgetMeta().forEach(m => { metaById[m.id] = m; });

        const tools = `<div class="dash-tools">${Components.dateRangeBar(this._dateRange)}`
            + `<button class="btn ${edit ? 'btn-primary' : 'btn-secondary'}" id="dash-customize">${edit ? '✓ Done' : '⚙ Customize'}</button></div>`;
        const hint = edit ? '<div class="dash-edit-hint">Drag to reorder · ⤢ resize · \u{1F43E} platforms · × remove · or add a widget below.</div>' : '';

        const cells = layout.map(w => {
            // The platform button only appears on widgets that actually have a
            // platform dimension (see _PLATFORM_FILTERABLE) — offering it on
            // "Top fans" or "Quick actions" would imply a filter that does
            // nothing.
            const canFilter = this._PLATFORM_FILTERABLE.indexOf(w.id) !== -1;
            const filtered = this._widgetExclude(w).length > 0;
            const ctl = edit
                ? `<div class="dash-wctl">`
                    + (canFilter ? `<button class="dash-wplat${filtered ? ' is-filtered' : ''}" data-wplat="${w.id}" title="Choose platforms">\u{1F43E}</button>` : '')
                    + `<button class="dash-wsize" data-wsz="${w.id}" title="Resize">⤢</button>`
                    + `<button class="dash-wrm" data-wrm="${w.id}" title="Remove">×</button></div>`
                : '';
            return `<div class="dash-w" data-span="${w.span}" data-wid="${w.id}"${edit ? ' draggable="true"' : ''}>${ctl}${this._dashWidgetHtml(w.id, ctx, w)}</div>`;
        }).join('');
        const addTile = edit ? '<button class="dash-addw" id="dash-addw"><span class="dash-addw-plus">+</span>Add widget</button>' : '';

        const html = `${this._refreshIndicatorHtml()}`
            + `<div class="page-header"><h2>Overview</h2>${tools}</div>${hint}`
            + `<div class="dash-grid${edit ? ' editing' : ''}" id="dash-grid">${cells}${addTile}</div>`;

        Charts.destroyAll();
        this._setContent(html);

        layout.forEach(w => this._dashWidgetMount(w.id, ctx, w));

        // Chart-type toggle (Workbench · Bento) — persists per-widget cfg.
        document.querySelectorAll('[data-chart-type]').forEach(b => b.addEventListener('click', (e) => {
            e.stopPropagation();
            const t = b.getAttribute('data-chart-type') === 'bar' ? 'bar' : 'line';
            const cw = (this._dashboardLayout || []).find(x => x.id === 'charts');
            if (!cw) return;
            cw.cfg = Object.assign({}, cw.cfg, { chartType: t });
            this._saveDashLayout();
            this._renderDashboard();
        }));

        document.getElementById('dash-customize')?.addEventListener('click', () => {
            this._dashEdit = !this._dashEdit;
            this._renderDashboard();
        });
        document.getElementById('dash-addw')?.addEventListener('click', () => this._openDashCatalog());
        document.querySelectorAll('[data-wrm]').forEach(b => b.addEventListener('click', (e) => {
            e.stopPropagation();
            this._dashboardLayout = this._dashboardLayout.filter(w => w.id !== b.getAttribute('data-wrm'));
            this._saveDashLayout();
            this._renderDashboard();
        }));
        document.querySelectorAll('[data-wsz]').forEach(b => b.addEventListener('click', (e) => {
            e.stopPropagation();
            const id = b.getAttribute('data-wsz');
            const w = this._dashboardLayout.find(x => x.id === id);
            const m = metaById[id];
            if (w && m) { w.span = m.spans[(m.spans.indexOf(w.span) + 1) % m.spans.length]; this._saveDashLayout(); this._renderDashboard(); }
        }));
        document.querySelectorAll('[data-wplat]').forEach(b => b.addEventListener('click', (e) => {
            e.stopPropagation();
            this._openPlatformPicker(b.getAttribute('data-wplat'));
        }));
        this._wireDashDrag();
        this._bindDateRange(() => this.renderOverview());
    },

    _wireDashDrag() {
        const grid = document.getElementById('dash-grid');
        if (!grid) return;
        let dragId = null;
        grid.querySelectorAll('.dash-w[draggable]').forEach(el => {
            el.addEventListener('dragstart', () => { dragId = el.getAttribute('data-wid'); el.classList.add('dragging'); });
            el.addEventListener('dragend', () => { el.classList.remove('dragging'); grid.querySelectorAll('.dragover').forEach(x => x.classList.remove('dragover')); });
            el.addEventListener('dragover', (e) => { e.preventDefault(); el.classList.add('dragover'); });
            el.addEventListener('dragleave', () => el.classList.remove('dragover'));
            el.addEventListener('drop', (e) => {
                e.preventDefault();
                el.classList.remove('dragover');
                const target = el.getAttribute('data-wid');
                if (!dragId || dragId === target) return;
                const L = this._dashboardLayout;
                const from = L.findIndex(w => w.id === dragId);
                const to = L.findIndex(w => w.id === target);
                if (from < 0 || to < 0) return;
                const [moved] = L.splice(from, 1);
                L.splice(to, 0, moved);
                this._saveDashLayout();
                this._renderDashboard();
            });
        });
    },

    _saveDashLayout() {
        API.savePreferences({ dashboard_layout: this._dashboardLayout }).catch(() => { /* best-effort */ });
    },

    _openDashCatalog() {
        const meta = this._dashWidgetMeta();
        const have = new Set((this._dashboardLayout || []).map(w => w.id));
        const cards = meta.map(m => `
            <button class="dash-catcard${have.has(m.id) ? ' in' : ''}" data-cat="${m.id}"${have.has(m.id) ? ' disabled' : ''}>
                <span class="dash-catico">${m.icon}</span>
                <span class="dash-catmeta"><b>${m.title}</b><span>${m.desc}</span></span>
                ${have.has(m.id) ? '<span class="dash-cattick">✓</span>' : ''}
            </button>`).join('');
        let ov = document.getElementById('dash-catalog');
        if (!ov) {
            ov = document.createElement('div');
            ov.id = 'dash-catalog';
            ov.className = 'dash-catalog-ov';
            document.body.appendChild(ov);
        }
        ov.innerHTML = `<div class="dash-catalog"><h3>Add a widget</h3><p>Pick something to add to your dashboard.</p><div class="dash-cat">${cards}</div></div>`;
        ov.classList.add('open');
        ov.onclick = (e) => { if (e.target === ov) ov.classList.remove('open'); };
        ov.querySelectorAll('[data-cat]').forEach(b => b.addEventListener('click', () => {
            const id = b.getAttribute('data-cat');
            if (this._dashboardLayout.some(w => w.id === id)) return;
            const m = meta.find(x => x.id === id);
            this._dashboardLayout.push({ id, span: m.spans[0] });
            this._saveDashLayout();
            ov.classList.remove('open');
            this._renderDashboard();
        }));
    },

    /* _openPlatformPicker(widgetId) — choose which platforms one widget counts.
     *
     * Reuses the widget-catalog overlay. Checked = included; the layout stores
     * the UNCHECKED codes in cfg.exclude, so a platform added to PawPoller
     * later is automatically counted by every existing widget (see
     * _widgetExclude). Clearing every box would leave a widget showing nothing,
     * so "None" is deliberately not offered — "All" resets instead.
     *
     * Platforms with no data yet still appear: filtering is about what you want
     * counted, not about what happens to have been polled. */
    _openPlatformPicker(widgetId) {
        const w = (this._dashboardLayout || []).find(x => x.id === widgetId);
        if (!w) return;
        const meta = this._dashWidgetMeta().find(m => m.id === widgetId);
        const exclude = new Set(this._widgetExclude(w));
        const rollup = {};
        ((this._dashCtx && this._dashCtx.platRollup) || []).forEach(p => { rollup[p.code] = p; });

        const rows = window.PLATFORMS.map(p => {
            const on = !exclude.has(p.code);
            const r = rollup[p.code] || {};
            // The sub-count is the useful hint here — it tells you at a glance
            // which platforms would actually change the number.
            const sub = r.subs ? `${Utils.formatCompact(r.subs)} works` : 'nothing polled yet';
            return `<label class="dash-platpick${on ? ' on' : ''}">`
                + `<input type="checkbox" data-plat="${p.code}"${on ? ' checked' : ''}>`
                + `<span class="dash-platpick-em">${p.emoji || ''}</span>`
                + `<span class="dash-platpick-meta"><b>${Utils.escapeHtml(p.label)}</b>`
                + `<span>${Utils.escapeHtml(sub)}</span></span></label>`;
        }).join('');

        let ov = document.getElementById('dash-catalog');
        if (!ov) {
            ov = document.createElement('div');
            ov.id = 'dash-catalog';
            ov.className = 'dash-catalog-ov';
            document.body.appendChild(ov);
        }
        ov.innerHTML = `<div class="dash-catalog"><h3>Platforms in “${Utils.escapeHtml(meta ? meta.title : widgetId)}”</h3>`
            + `<p>Only the ticked platforms count towards this widget. Newly connected platforms are included automatically.</p>`
            + `<div class="dash-platpick-tools"><button class="btn btn-secondary" id="platpick-all">Select all</button>`
            + `<button class="btn btn-secondary" id="platpick-invert">Invert</button></div>`
            + `<div class="dash-platpicks">${rows}</div>`
            + `<div class="dash-platpick-foot"><button class="btn btn-primary" id="platpick-done">Done</button></div></div>`;
        ov.classList.add('open');

        const apply = () => {
            const off = [];
            ov.querySelectorAll('[data-plat]').forEach(cb => {
                if (!cb.checked) off.push(cb.getAttribute('data-plat'));
                cb.closest('.dash-platpick')?.classList.toggle('on', cb.checked);
            });
            // Every box unticked means "show nothing", which is never what the
            // user wants from a stat card — treat it as no filter at all.
            const cfg = Object.assign({}, w.cfg);
            if (off.length && off.length < window.PLATFORMS.length) cfg.exclude = off;
            else delete cfg.exclude;
            w.cfg = Object.keys(cfg).length ? cfg : undefined;
            this._saveDashLayout();
        };

        ov.querySelectorAll('[data-plat]').forEach(cb => cb.addEventListener('change', apply));
        ov.querySelector('#platpick-all')?.addEventListener('click', () => {
            ov.querySelectorAll('[data-plat]').forEach(cb => { cb.checked = true; });
            apply();
        });
        ov.querySelector('#platpick-invert')?.addEventListener('click', () => {
            ov.querySelectorAll('[data-plat]').forEach(cb => { cb.checked = !cb.checked; });
            apply();
        });
        const close = () => { ov.classList.remove('open'); this._renderDashboard(); };
        ov.querySelector('#platpick-done')?.addEventListener('click', close);
        ov.onclick = (e) => { if (e.target === ov) close(); };
    },

    /* ── Dashboard Overview ────────────────────────────────────
     * renderDashboard() — Inkbunny-specific dashboard (the default landing page).
     *
     * Fetches IB summary stats and aggregate snapshots in parallel. Renders:
     * stat cards (submissions, views, faves, comments), growth rate cards
     * (1h/24h/7d/30d deltas), date-range-filtered aggregate views-over-time
     * line chart, top viewed and top faved horizontal bar charts, fastest
     * growing list (24h views gained), recent fave activity, and recent
     * comments. Binds date range bar and starts auto-refresh. */

    async renderDashboard() {
        this._loading();
        try {
            /* Fetch IB summary stats, aggregate snapshots, pins, and goals in parallel.
               account_id (null = All accounts) scopes summary + aggregate to the
               account picked in the context bar; pins/goals stay platform-wide. */
            const acc = this._acctId('ib');
            const [summary, agg, pins, goals] = await Promise.all([
                API.getSummary({ account_id: acc }),
                API.getAggregate({ ...Utils.getDateRange(this._dateRange), account_id: acc }),
                API.getPins().catch(() => ({ pins: [] })),
                API.getGoals().catch(() => ({ goals: [] })),
            ]);
            const ibPins = (pins.pins || []).filter(p => p.platform === 'ib');
            const ibGoals = (goals.goals || []).filter(g => g.platform === 'ib' || g.platform === 'all');

            // Empty-state short-circuit: see SF dashboard for the pattern.
            const ibHealth = window.PlatformHealth && window.PlatformHealth.get('ib');
            const isUnconfigured = ibHealth && ibHealth.configured === false;
            if (isUnconfigured || (summary.total_submissions || 0) === 0) {
                this._setContent(`
                    ${this._refreshIndicatorHtml()}
                    <div class="page-header"><h2>Inkbunny Dashboard</h2></div>
                    ${Components.platformEmptyState('ib', isUnconfigured ? {} : { reason: 'Inkbunny is configured but no submissions have been polled yet. The first poll may still be running.' })}
                `);
                return;
            }

            const html = `
                ${this._refreshIndicatorHtml()}
                <div class="page-header">
                    <h2>Inkbunny Dashboard</h2>
                    <div style="display:flex;gap:8px">
                        <button class="btn btn-primary" data-poll="ib">Poll Now</button>
                        <button class="btn btn-secondary" data-resync="ib">Full Resync</button>
                    </div>
                </div>

                ${ibPins.length ? Components.pinnedSubmissions(ibPins, 'ib') : ''}
                ${ibGoals.length ? `<div class="goals-section"><h3>Goals</h3>${Components.goalProgressCards(ibGoals)}</div>` : ''}

                <div class="stats-grid">
                    ${Components.statCard('Total Submissions', summary.total_submissions, null, '#/ib/submissions')}
                    ${Components.statCard('Total Views', summary.total_views)}
                    ${Components.statCard('Total Favorites', summary.total_favorites)}
                    ${Components.statCard('Total Comments', summary.total_comments)}
                    ${Components.statCard('Total Watchers', summary.total_watchers || 0)}
                </div>

                ${Components.growthRateCards(summary.growth_rates)}

                ${Components.dateRangeBar(this._dateRange)}

                <div class="chart-container">
                    <h3>Views Over Time (Aggregate)</h3>
                    <div class="chart-wrap"><canvas id="chart-agg-views"></canvas></div>
                </div>

                <div class="chart-row">
                    <div class="chart-container">
                        <h3>Top Viewed</h3>
                        <div class="chart-wrap"><canvas id="chart-top-views"></canvas></div>
                    </div>
                    <div class="chart-container">
                        <h3>Top Faved</h3>
                        <div class="chart-wrap"><canvas id="chart-top-faves"></canvas></div>
                    </div>
                </div>

                <div class="chart-row">
                    <div class="chart-container">
                        <h3>Fastest Growing (24h)</h3>
                        ${Components.topList(summary.fastest_growing, 'views_gained')}
                    </div>
                    <div class="chart-container">
                        <h3>Recent Fave Activity</h3>
                        ${Components.recentFaves(summary.recent_faves)}
                    </div>
                </div>

                <div class="chart-row">
                    <div class="chart-container">
                        <h3>Recent Comments</h3>
                        ${Components.recentComments(summary.recent_comments)}
                    </div>
                    <div class="chart-container">
                        <h3>Recent Watchers</h3>
                        ${Components.recentWatchers(summary.recent_watchers)}
                    </div>
                </div>
            `;

            this._setContent(html);

            // Render charts
            if (agg.snapshots && agg.snapshots.length > 0) {
                Charts.aggregateLine('chart-agg-views', agg.snapshots, ['views']);
            }
            if (summary.top_viewed && summary.top_viewed.length > 0) {
                Charts.topBar('chart-top-views', summary.top_viewed, 'views');
            }
            if (summary.top_faved && summary.top_faved.length > 0) {
                Charts.topBar('chart-top-faves', summary.top_faved, 'favorites_count');
            }

            // Date range click handler
            this._bindDateRange(() => this.renderDashboard());
            this._bindPinAndGoalActions(() => this.renderDashboard());

            this._startAutoRefresh(() => this.renderDashboard());
        } catch (err) {
            this._setContent(`<div class="empty-state"><h3>Error loading dashboard</h3><p>${Utils.escapeHtml(err.message)}</p></div>`);
        }
    },

    /* ── Submissions Table ─────────────────────────────────────
     * renderSubmissions() — IB submissions table with search, rating filter,
     * and type filter. Loads all submissions sorted by _sortState. Binds
     * column-header sort (_bindTableSort) and client-side search/filter
     * (_bindSearch). Starts auto-refresh. */

    async renderSubmissions() {
        this._loading();
        try {
            const data = await API.getSubmissions({
                sort_by: this._sortState.field,
                order: this._sortState.order,
                account_id: this._acctId('ib'),
            });

            const _vm = localStorage.getItem('pp-view-mode') || 'grid';
            // 2.16.14 (BUG-021): grid render extracted to a closure so the
            // search filter can re-render it. Previously _bindSearch only
            // updated #table-container, but most pages default to grid view
            // — so typing in the search box appeared to do nothing.
            const ibGridRenderer = (subs) => Components.submissionCardGrid(subs, {
                idKey: 'submission_id', titleKey: 'title', thumbKey: 'thumb_url',
                detailRoute: '/ib/submission', dateKey: 'create_datetime',
                stats: [
                    { key: 'views', deltaKey: 'views_delta', label: 'views' },
                    { key: 'favorites_count', deltaKey: 'faves_delta', label: 'faves' },
                    { key: 'comments_count', deltaKey: 'comments_delta', label: 'comments' },
                ],
            });
            const gridHtml = ibGridRenderer(data.submissions);
            const html = `
                ${this._refreshIndicatorHtml()}
                <div class="page-header"><h2>Submissions</h2></div>
                <div class="toolbar">
                    <input type="text" class="search-input" id="search-input" placeholder="Search titles and keywords...">
                    <select class="filter-select" id="filter-rating">
                        <option value="">All Ratings</option>
                        <option value="0">General</option>
                        <option value="1">Mature</option>
                        <option value="2">Adult</option>
                    </select>
                    <select class="filter-select" id="filter-type">
                        <option value="">All Types</option>
                        <option value="Picture/Pinup">Picture</option>
                        <option value="Writing - Document">Writing</option>
                        <option value="Music - Single Track">Music</option>
                    </select>
                    <div class="view-toggle">
                        <button class="view-toggle-btn ${_vm === 'grid' ? 'active' : ''}" data-view="grid" title="Grid view">&#9638;</button>
                        <button class="view-toggle-btn ${_vm === 'list' ? 'active' : ''}" data-view="list" title="List view">&#9776;</button>
                    </div>
                </div>
                <div id="grid-container" style="${_vm !== 'grid' ? 'display:none' : ''}">${gridHtml}</div>
                <div id="table-container" class="table-scroll" style="${_vm !== 'list' ? 'display:none' : ''}">
                    ${Components.submissionsTable(data.submissions)}
                </div>
            `;

            this._setContent(html);
            this._bindViewToggle();
            this._bindTableSort();
            this._bindSearch(data.submissions, ibGridRenderer);

            this._startAutoRefresh(() => this.renderSubmissions());
        } catch (err) {
            this._setContent(`<div class="empty-state"><h3>Error loading submissions</h3><p>${Utils.escapeHtml(err.message)}</p></div>`);
        }
    },

    /* ── Submission Detail — IB detail: thumbnail, metadata, stats, growth rates,
     * time-series chart, faving users table, comments. Date range re-fetches snapshots only. */

    async renderDetail(id) {
        this._loading();
        try {
            const [data, pins, allTags] = await Promise.all([
                API.getSubmission(id),
                API.getPins().catch(() => ({ pins: [] })),
                API.getTags().catch(() => ({ tags: [] })),
            ]);
            const sub = data.submission;
            const isPinned = (pins.pins || []).some(p => p.platform === 'ib' && p.submission_id === id);
            const currentTags = sub.tags || [];

            const html = `
                ${this._refreshIndicatorHtml()}
                <a href="#/ib/submissions" class="back-link">&larr; Back to Submissions</a>
                <div class="detail-header">
                    ${sub.thumb_url ? `<img src="${Utils.thumbUrl(sub.thumb_url)}" class="detail-thumb">` : ''}
                    <div class="detail-info">
                        <h2>${Utils.escapeHtml(sub.title)}</h2>
                        <div class="detail-meta">by ${Utils.escapeHtml(sub.username)} &middot; ${Utils.formatDate(sub.create_datetime)} &middot; ${Utils.escapeHtml(sub.type_name)} &middot; ${Utils.escapeHtml(sub.rating_name)}</div>
                        <div class="detail-meta"><a href="https://inkbunny.net/s/${sub.submission_id}" target="_blank">View on Inkbunny</a></div>
                        <div class="detail-stats">
                            <div class="detail-stat">${Utils.formatNumber(sub.views)} <span class="lbl">views</span></div>
                            <div class="detail-stat">${Utils.formatNumber(sub.favorites_count)} <span class="lbl">faves</span></div>
                            <div class="detail-stat">${Utils.formatNumber(sub.comments_count)} <span class="lbl">comments</span></div>
                        </div>
                        <div style="margin-top:8px;display:flex;gap:8px;align-items:center;flex-wrap:wrap">
                            <button class="btn ${isPinned ? 'btn-danger' : 'btn-secondary'} btn-pin" data-platform="ib" data-id="${id}" style="padding:4px 10px;font-size:12px">${isPinned ? 'Unpin' : 'Pin'}</button>
                            ${currentTags.map(t => Components.tagBadge(t)).join('')}
                            <button class="btn btn-secondary btn-add-tag" data-platform="ib" data-id="${id}" style="padding:4px 10px;font-size:12px">+ Tag</button>
                        </div>
                        <div style="margin-top:8px">${Components.keywords(sub.keywords)}</div>
                    </div>
                </div>

                ${Components.growthRateCards(data.growth_rates)}

                ${Components.dateRangeBar(this._dateRange)}

                <div class="chart-container">
                    <h3>Stats Over Time</h3>
                    <div class="chart-wrap"><canvas id="chart-detail"></canvas></div>
                </div>

                <div class="chart-container">
                    <h3>Faving Users (${data.faving_users.length})</h3>
                    ${Components.favingUsersTable(data.faving_users)}
                </div>

                <div class="chart-container">
                    <h3>Comments (${(data.comments || []).length})</h3>
                    ${Components.commentsSection(data.comments)}
                </div>
            `;

            this._setContent(html);

            if (data.snapshots && data.snapshots.length > 0) {
                Charts.submissionLine('chart-detail', data.snapshots);
            }

            /* Date range changes re-fetch only snapshots for the chart, not the full detail */
            this._bindDateRange(async () => {
                const range = Utils.getDateRange(this._dateRange);
                const snaps = await API.getSnapshots(id, range);
                Charts.submissionLine('chart-detail', snaps.snapshots);
            });

            this._bindDetailPinTag('ib', id, allTags.tags || [], () => this.renderDetail(id));
            this._startAutoRefresh(() => this.renderDetail(id));
        } catch (err) {
            this._setContent(`<div class="empty-state"><h3>Error loading submission</h3><p>${Utils.escapeHtml(err.message)}</p></div>`);
        }
    },

    /* ── Comparison — IB comparison page: chip selection (max 5), metric
     * dropdown, date range, overlay chart. Chips toggle _compareIds; metric/
     * date changes re-render only the chart via _loadComparisonChart(). */

    async renderCompare() {
        this._loading();
        try {
            const data = await API.getSubmissions({ sort_by: 'views', order: 'desc', account_id: this._acctId('ib') });
            const subs = data.submissions;

            /* Build chip labels for every submission; pre-check selected state from _compareIds */
            const chips = subs.map(s => `
                <label class="compare-chip ${this._compareIds.has(s.submission_id) ? 'selected' : ''}" data-id="${s.submission_id}">
                    <input type="checkbox" ${this._compareIds.has(s.submission_id) ? 'checked' : ''}>
                    ${Utils.escapeHtml(Utils.truncate(s.title, 25))}
                </label>
            `).join('');

            const html = `
                ${this._refreshIndicatorHtml()}
                <div class="page-header">
                    <h2>Compare Submissions</h2>
                    <div>
                        <select class="filter-select" id="compare-metric">
                            <option value="views" ${this._compareMetric === 'views' ? 'selected' : ''}>Views</option>
                            <option value="favorites_count" ${this._compareMetric === 'favorites_count' ? 'selected' : ''}>Favourites</option>
                            <option value="comments_count" ${this._compareMetric === 'comments_count' ? 'selected' : ''}>Comments</option>
                        </select>
                    </div>
                </div>
                <p style="font-size:13px;color:var(--text-muted);margin-bottom:12px">Compare how your submissions perform over time. Select 2-5 submissions and choose a metric (views, favourites, or comments) to see their trends side by side.</p>
                <div class="compare-select">${chips}</div>

                ${Components.dateRangeBar(this._dateRange)}

                <div class="chart-container" id="compare-chart-container" style="${this._compareIds.size < 2 ? 'display:none' : ''}">
                    <h3>Comparison</h3>
                    <div class="chart-wrap"><canvas id="chart-compare"></canvas></div>
                </div>
                ${this._compareIds.size < 2 ? '<div class="empty-state"><p>Select at least 2 submissions above to see their trends compared.</p></div>' : ''}
            `;

            this._setContent(html);

            /* Chip click handlers: toggle selection in _compareIds (max 5), re-render page */
            document.querySelectorAll('.compare-chip').forEach(chip => {
                chip.addEventListener('click', (e) => {
                    e.preventDefault();
                    const id = parseInt(chip.dataset.id);
                    if (this._compareIds.has(id)) {
                        this._compareIds.delete(id);
                    } else if (this._compareIds.size < 5) {
                        this._compareIds.add(id);
                    }
                    this.renderCompare();
                });
            });

            /* Metric dropdown: changing it stores the value and re-renders only the chart */
            const metricSelect = document.getElementById('compare-metric');
            if (metricSelect) {
                metricSelect.addEventListener('change', () => {
                    this._compareMetric = metricSelect.value;
                    this._loadComparisonChart();
                });
            }

            this._bindDateRange(() => this._loadComparisonChart());

            if (this._compareIds.size >= 2) {
                await this._loadComparisonChart();
            }

            this._startAutoRefresh(() => this.renderCompare());
        } catch (err) {
            this._setContent(`<div class="empty-state"><h3>Error</h3><p>${Utils.escapeHtml(err.message)}</p></div>`);
        }
    },

    /* _loadComparisonChart() — Fetches comparison data for selected IB submissions
     * (_compareIds) and renders an overlay line chart. Bails out if fewer than 2
     * submissions are selected. Shows the compare-chart-container div and delegates
     * to Charts.comparisonLine() with the current _compareMetric. */
    async _loadComparisonChart() {
        try {
            if (this._compareIds.size < 2) return;
            const range = Utils.getDateRange(this._dateRange);
            const data = await API.getComparison([...this._compareIds], range);
            const container = document.getElementById('compare-chart-container');
            if (container) container.style.display = '';
            Charts.comparisonLine('chart-compare', data.series, data.titles, this._compareMetric);
        } catch (e) {
            console.error('Failed to load comparison chart:', e);
        }
    },

    // ── FA Dashboard ───────────────────────────────────────────
    // FurAffinity-specific dashboard page. Follows the same pattern as the IB
    // dashboard but uses FA API methods (getFASummary, getFAAggregate) and FA
    // components (faTopList, faRecentComments). Displays summary stat cards
    // (submissions, views, favourites, comments), growth rate cards, aggregate
    // views-over-time chart, top viewed/faved bar charts, fastest-growing list,
    // and a recent comments panel.

    async renderFADashboard() {
        this._loading();
        try {
            const [summary, agg, pins, goals] = await Promise.all([
                API.getFASummary({ account_id: this._acctId('fa') }),
                API.getFAAggregate({ ...Utils.getDateRange(this._dateRange), account_id: this._acctId('fa') }),
                API.getPins().catch(() => ({ pins: [] })),
                API.getGoals().catch(() => ({ goals: [] })),
            ]);
            const faPins = (pins.pins || []).filter(p => p.platform === 'fa');
            const faGoals = (goals.goals || []).filter(g => g.platform === 'fa' || g.platform === 'all');

            // Empty-state short-circuit: see SF dashboard for the pattern.
            const faHealth = window.PlatformHealth && window.PlatformHealth.get('fa');
            const isUnconfigured = faHealth && faHealth.configured === false;
            if (isUnconfigured || (summary.total_submissions || 0) === 0) {
                this._setContent(`
                    ${this._refreshIndicatorHtml()}
                    <div class="page-header"><h2>FurAffinity Dashboard</h2></div>
                    ${Components.platformEmptyState('fa', isUnconfigured ? {} : { reason: 'FurAffinity is configured but no submissions have been polled yet. The first poll may still be running.' })}
                `);
                return;
            }

            const html = `
                ${this._refreshIndicatorHtml()}
                <div class="page-header">
                    <h2>FurAffinity Dashboard</h2>
                    <div style="display:flex;gap:8px">
                        <button class="btn btn-primary" data-poll="fa">Poll Now</button>
                        <button class="btn btn-secondary" data-resync="fa">Full Resync</button>
                    </div>
                </div>

                ${faPins.length ? Components.pinnedSubmissions(faPins, 'fa') : ''}
                ${faGoals.length ? `<div class="goals-section"><h3>Goals</h3>${Components.goalProgressCards(faGoals)}</div>` : ''}

                <div class="stats-grid">
                    ${Components.statCard('Total Submissions', summary.total_submissions, null, '#/fa/submissions')}
                    ${Components.statCard('Total Views', summary.total_views)}
                    ${Components.statCard('Total Favorites', summary.total_favorites)}
                    ${Components.statCard('Total Comments', summary.total_comments)}
                    ${Components.statCard('Total Watchers', summary.total_watchers || 0)}
                    ${Components.statCard('Profile Views', summary.profile_pageviews || 0)}
                </div>

                ${Components.growthRateCards(summary.growth_rates)}

                ${Components.dateRangeBar(this._dateRange)}

                <div class="chart-container">
                    <h3>Views Over Time (Aggregate)</h3>
                    <div class="chart-wrap"><canvas id="chart-agg-views"></canvas></div>
                </div>

                <div class="chart-row">
                    <div class="chart-container">
                        <h3>Top Viewed</h3>
                        <div class="chart-wrap"><canvas id="chart-top-views"></canvas></div>
                    </div>
                    <div class="chart-container">
                        <h3>Top Faved</h3>
                        <div class="chart-wrap"><canvas id="chart-top-faves"></canvas></div>
                    </div>
                </div>

                <div class="chart-row">
                    <div class="chart-container">
                        <h3>Fastest Growing (24h)</h3>
                        ${Components.faTopList(summary.fastest_growing, 'views_gained')}
                    </div>
                    <div class="chart-container">
                        <h3>Recent Comments</h3>
                        ${Components.faRecentComments(summary.recent_comments)}
                    </div>
                </div>

                <div class="chart-row">
                    <div class="chart-container">
                        <h3>Recent Watchers</h3>
                        ${Components.recentWatchers(summary.recent_watchers)}
                    </div>
                </div>
            `;

            this._setContent(html);

            // Render charts only when data exists to avoid empty canvas errors
            if (agg.snapshots && agg.snapshots.length > 0) {
                Charts.aggregateLine('chart-agg-views', agg.snapshots, ['views']);
            }
            if (summary.top_viewed && summary.top_viewed.length > 0) {
                Charts.topBar('chart-top-views', summary.top_viewed, 'views');
            }
            if (summary.top_faved && summary.top_faved.length > 0) {
                Charts.topBar('chart-top-faves', summary.top_faved, 'favorites_count');
            }

            this._bindDateRange(() => this.renderFADashboard());
            this._bindPinAndGoalActions(() => this.renderFADashboard());
            this._startAutoRefresh(() => this.renderFADashboard());
        } catch (err) {
            this._setContent(`<div class="empty-state"><h3>Error loading FA dashboard</h3><p>${Utils.escapeHtml(err.message)}</p></div>`);
        }
    },

    // ── FA Submissions ─────────────────────────────────────────
    // FA submissions table with text search (title/keywords) and rating filter
    // dropdown (General/Mature/Adult). Sorting is handled server-side via
    // _faSortState. Search/filter is client-side against the full data set.

    async renderFASubmissions() {
        this._loading();
        try {
            // Fetch all FA submissions, sorted according to current sort state
            const data = await API.getFASubmissions({
                sort_by: this._faSortState.field,
                order: this._faSortState.order,
                account_id: this._acctId('fa'),
            });

            const _vm = localStorage.getItem('pp-view-mode') || 'grid';
            // 2.16.14 (BUG-021): closure so the search filter can re-render
            const faGridRenderer = (subs) => Components.submissionCardGrid(
                subs.map(s => ({ ...s, _thumb: s.thumbnail_url ? Utils.faThumbUrl(s.thumbnail_url) : null })),
                {
                    idKey: 'submission_id', titleKey: 'title', thumbKey: '_thumb',
                    detailRoute: '/fa/submission', dateKey: 'posted_at',
                    proxyThumb: false,
                    stats: [
                        { key: 'views', deltaKey: 'views_delta', label: 'views' },
                        { key: 'favorites_count', deltaKey: 'faves_delta', label: 'faves' },
                        { key: 'comments_count', deltaKey: 'comments_delta', label: 'comments' },
                    ],
                }
            );
            const gridHtml = faGridRenderer(data.submissions);
            const html = `
                ${this._refreshIndicatorHtml()}
                <div class="page-header"><h2>FA Submissions</h2></div>
                <div class="toolbar">
                    <input type="text" class="search-input" id="search-input" placeholder="Search titles and keywords...">
                    <select class="filter-select" id="filter-rating">
                        <option value="">All Ratings</option>
                        <option value="General">General</option>
                        <option value="Mature">Mature</option>
                        <option value="Adult">Adult</option>
                    </select>
                    <div class="view-toggle">
                        <button class="view-toggle-btn ${_vm === 'grid' ? 'active' : ''}" data-view="grid" title="Grid view">&#9638;</button>
                        <button class="view-toggle-btn ${_vm === 'list' ? 'active' : ''}" data-view="list" title="List view">&#9776;</button>
                    </div>
                </div>
                <div id="grid-container" style="${_vm !== 'grid' ? 'display:none' : ''}">${gridHtml}</div>
                <div id="table-container" class="table-scroll" style="${_vm !== 'list' ? 'display:none' : ''}">
                    ${Components.faSubmissionsTable(data.submissions)}
                </div>
            `;

            this._setContent(html);
            this._bindViewToggle();
            this._bindFATableSort();
            this._bindFASearch(data.submissions, faGridRenderer);
            this._startAutoRefresh(() => this.renderFASubmissions());
        } catch (err) {
            this._setContent(`<div class="empty-state"><h3>Error loading FA submissions</h3><p>${Utils.escapeHtml(err.message)}</p></div>`);
        }
    },

    // ── FA Submission Detail ───────────────────────────────────
    // Individual FA submission detail page. Shows header with thumbnail, title,
    // author, post date, rating, and a direct link to FurAffinity. Includes
    // FA-specific metadata grid (category, theme, species, gender) that IB does
    // not have. Displays keywords, growth rate cards, stats-over-time chart,
    // and a comments section. Unlike IB detail, there is no faving-users list
    // because the FA API does not expose who faved a submission.

    async renderFADetail(id) {
        this._loading();
        try {
            const [data, pins, allTags] = await Promise.all([
                API.getFASubmission(id),
                API.getPins().catch(() => ({ pins: [] })),
                API.getTags().catch(() => ({ tags: [] })),
            ]);
            const sub = data.submission;
            const isPinned = (pins.pins || []).some(p => p.platform === 'fa' && p.submission_id === id);
            const currentTags = sub.tags || [];

            const html = `
                ${this._refreshIndicatorHtml()}
                <a href="#/fa/submissions" class="back-link">&larr; Back to FA Submissions</a>
                <div class="detail-header">
                    ${sub.thumbnail_url ? `<img src="${Utils.faThumbUrl(sub.thumbnail_url)}" class="detail-thumb">` : ''}
                    <div class="detail-info">
                        <h2>${Utils.escapeHtml(sub.title)}</h2>
                        <div class="detail-meta">by ${Utils.escapeHtml(sub.username)} &middot; ${Utils.formatDate(sub.posted_at)} &middot; ${Utils.escapeHtml(sub.rating || '')}</div>
                        <div class="detail-meta"><a href="${Utils.escapeHtml(Utils.safeUrl(sub.link) || '#')}" target="_blank">View on FurAffinity</a></div>
                        <div class="detail-stats">
                            <div class="detail-stat">${Utils.formatNumber(sub.views)} <span class="lbl">views</span></div>
                            <div class="detail-stat">${Utils.formatNumber(sub.favorites_count)} <span class="lbl">faves</span></div>
                            <div class="detail-stat">${Utils.formatNumber(sub.comments_count)} <span class="lbl">comments</span></div>
                        </div>
                        <div style="margin-top:8px;display:flex;gap:8px;align-items:center;flex-wrap:wrap">
                            <button class="btn ${isPinned ? 'btn-danger' : 'btn-secondary'} btn-pin" data-platform="fa" data-id="${id}" style="padding:4px 10px;font-size:12px">${isPinned ? 'Unpin' : 'Pin'}</button>
                            ${currentTags.map(t => Components.tagBadge(t)).join('')}
                            <button class="btn btn-secondary btn-add-tag" data-platform="fa" data-id="${id}" style="padding:4px 10px;font-size:12px">+ Tag</button>
                        </div>
                        <div class="fa-metadata">
                            ${sub.category ? `<div class="fa-meta-item"><span class="fa-meta-label">Category</span><span class="fa-meta-value">${Utils.escapeHtml(sub.category)}</span></div>` : ''}
                            ${sub.theme ? `<div class="fa-meta-item"><span class="fa-meta-label">Theme</span><span class="fa-meta-value">${Utils.escapeHtml(sub.theme)}</span></div>` : ''}
                            ${sub.species ? `<div class="fa-meta-item"><span class="fa-meta-label">Species</span><span class="fa-meta-value">${Utils.escapeHtml(sub.species)}</span></div>` : ''}
                            ${sub.gender ? `<div class="fa-meta-item"><span class="fa-meta-label">Gender</span><span class="fa-meta-value">${Utils.escapeHtml(sub.gender)}</span></div>` : ''}
                        </div>
                        <div style="margin-top:8px">${Components.keywords(sub.keywords)}</div>
                    </div>
                </div>

                ${Components.growthRateCards(data.growth_rates)}

                ${Components.dateRangeBar(this._dateRange)}

                <div class="chart-container">
                    <h3>Stats Over Time</h3>
                    <div class="chart-wrap"><canvas id="chart-detail"></canvas></div>
                </div>

                <div class="chart-container">
                    <h3>Comments (${(data.comments || []).length})</h3>
                    ${Components.faCommentsSection(data.comments)}
                </div>
            `;

            this._setContent(html);

            // Render the stats-over-time line chart if snapshot data is available
            if (data.snapshots && data.snapshots.length > 0) {
                Charts.submissionLine('chart-detail', data.snapshots);
            }

            // Date range changes only re-fetch snapshots (not the full submission),
            // then re-render just the chart for a faster update
            this._bindDateRange(async () => {
                const range = Utils.getDateRange(this._dateRange);
                const snaps = await API.getFASnapshots(id, range);
                Charts.submissionLine('chart-detail', snaps.snapshots);
            });

            this._bindDetailPinTag('fa', id, allTags.tags || [], () => this.renderFADetail(id));
            this._startAutoRefresh(() => this.renderFADetail(id));
        } catch (err) {
            this._setContent(`<div class="empty-state"><h3>Error loading FA submission</h3><p>${Utils.escapeHtml(err.message)}</p></div>`);
        }
    },

    // ── FA Compare ─────────────────────────────────────────────
    // FA comparison page. Same chip-selection / metric-dropdown / chart pattern
    // as the IB comparison page. Users select 2-5 FA submissions via toggle
    // chips, choose a metric (views/favourites/comments), and view a comparison
    // line chart. Selected submission IDs are tracked in _faCompareIds (a Set),
    // and the active metric lives in _faCompareMetric.

    async renderFACompare() {
        this._loading();
        try {
            // Fetch all FA submissions sorted by views to populate the chip selector
            const data = await API.getFASubmissions({ sort_by: 'views', order: 'desc', account_id: this._acctId('fa') });
            const subs = data.submissions;

            // Build selectable chips for each submission; pre-check any already selected
            const chips = subs.map(s => `
                <label class="compare-chip ${this._faCompareIds.has(s.submission_id) ? 'selected' : ''}" data-id="${s.submission_id}">
                    <input type="checkbox" ${this._faCompareIds.has(s.submission_id) ? 'checked' : ''}>
                    ${Utils.escapeHtml(Utils.truncate(s.title, 25))}
                </label>
            `).join('');

            const html = `
                ${this._refreshIndicatorHtml()}
                <div class="page-header">
                    <h2>Compare FA Submissions</h2>
                    <div>
                        <select class="filter-select" id="compare-metric">
                            <option value="views" ${this._faCompareMetric === 'views' ? 'selected' : ''}>Views</option>
                            <option value="favorites_count" ${this._faCompareMetric === 'favorites_count' ? 'selected' : ''}>Favourites</option>
                            <option value="comments_count" ${this._faCompareMetric === 'comments_count' ? 'selected' : ''}>Comments</option>
                        </select>
                    </div>
                </div>
                <p style="font-size:13px;color:var(--text-muted);margin-bottom:12px">Select 2-5 FA submissions to compare their trends over time.</p>
                <div class="compare-select">${chips}</div>

                ${Components.dateRangeBar(this._dateRange)}

                <div class="chart-container" id="compare-chart-container" style="${this._faCompareIds.size < 2 ? 'display:none' : ''}">
                    <h3>Comparison</h3>
                    <div class="chart-wrap"><canvas id="chart-compare"></canvas></div>
                </div>
                ${this._faCompareIds.size < 2 ? '<div class="empty-state"><p>Select at least 2 submissions above to see their trends compared.</p></div>' : ''}
            `;

            this._setContent(html);

            // Chip click handlers: toggle selection, cap at 5, re-render to update visual state
            document.querySelectorAll('.compare-chip').forEach(chip => {
                chip.addEventListener('click', (e) => {
                    e.preventDefault();
                    const id = parseInt(chip.dataset.id);
                    if (this._faCompareIds.has(id)) {
                        this._faCompareIds.delete(id);
                    } else if (this._faCompareIds.size < 5) {
                        this._faCompareIds.add(id);
                    }
                    this.renderFACompare();
                });
            });

            // Metric dropdown: changing metric re-fetches and redraws the chart
            const metricSelect = document.getElementById('compare-metric');
            if (metricSelect) {
                metricSelect.addEventListener('change', () => {
                    this._faCompareMetric = metricSelect.value;
                    this._loadFAComparisonChart();
                });
            }

            // Date range changes also reload the comparison chart
            this._bindDateRange(() => this._loadFAComparisonChart());

            // Immediately load chart if enough submissions are selected
            if (this._faCompareIds.size >= 2) {
                await this._loadFAComparisonChart();
            }

            this._startAutoRefresh(() => this.renderFACompare());
        } catch (err) {
            this._setContent(`<div class="empty-state"><h3>Error</h3><p>${Utils.escapeHtml(err.message)}</p></div>`);
        }
    },

    // Fetches FA comparison data for the selected submission IDs and renders
    // the multi-line comparison chart. Unhides the chart container if hidden.
    async _loadFAComparisonChart() {
        try {
            // Guard: need at least 2 submissions to draw a meaningful comparison
            if (this._faCompareIds.size < 2) return;
            const range = Utils.getDateRange(this._dateRange);
            // Spread the Set into an array for the API call
            const data = await API.getFAComparison([...this._faCompareIds], range);
            const container = document.getElementById('compare-chart-container');
            if (container) container.style.display = '';
            Charts.comparisonLine('chart-compare', data.series, data.titles, this._faCompareMetric);
        } catch (e) {
            console.error('Failed to load FA comparison chart:', e);
        }
    },

    // ── WS Dashboard ──────────────────────────────────────────
    // Weasyl-specific dashboard. Same layout as FA/IB dashboards: stat cards,
    // growth rates, aggregate chart, top viewed/faved charts, and fastest
    // growing list. Unlike FA, there is no recent comments section because
    // the Weasyl API does not expose individual comment text. Includes a
    // CSV export button in the header.

    async renderWSDashboard() {
        this._loading();
        try {
            const [summary, agg, pins, goals] = await Promise.all([
                API.getWSSummary({ account_id: this._acctId('ws') }),
                API.getWSAggregate({ ...Utils.getDateRange(this._dateRange), account_id: this._acctId('ws') }),
                API.getPins().catch(() => ({ pins: [] })),
                API.getGoals().catch(() => ({ goals: [] })),
            ]);
            const wsPins = (pins.pins || []).filter(p => p.platform === 'ws');
            const wsGoals = (goals.goals || []).filter(g => g.platform === 'ws' || g.platform === 'all');

            // Empty-state short-circuit: see SF dashboard for the pattern.
            const wsHealth = window.PlatformHealth && window.PlatformHealth.get('ws');
            const isUnconfigured = wsHealth && wsHealth.configured === false;
            if (isUnconfigured || (summary.total_submissions || 0) === 0) {
                this._setContent(`
                    ${this._refreshIndicatorHtml()}
                    <div class="page-header"><h2>Weasyl Dashboard</h2></div>
                    ${Components.platformEmptyState('ws', isUnconfigured ? {} : { reason: 'Weasyl is configured but no submissions have been polled yet. The first poll may still be running.' })}
                `);
                return;
            }

            const html = `
                ${this._refreshIndicatorHtml()}
                <div class="page-header">
                    <h2>Weasyl Dashboard</h2>
                    <div style="display:flex;gap:8px">
                        <button class="btn btn-primary" data-poll="ws">Poll Now</button>
                        <button class="btn btn-secondary" data-resync="ws">Full Resync</button>
                        <button class="btn btn-secondary" data-export="ws">Export CSV</button>
                    </div>
                </div>

                ${wsPins.length ? Components.pinnedSubmissions(wsPins, 'ws') : ''}
                ${wsGoals.length ? `<div class="goals-section"><h3>Goals</h3>${Components.goalProgressCards(wsGoals)}</div>` : ''}

                <div class="stats-grid">
                    ${Components.statCard('Total Submissions', summary.total_submissions, null, '#/ws/submissions')}
                    ${Components.statCard('Total Views', summary.total_views)}
                    ${Components.statCard('Total Favorites', summary.total_favorites)}
                    ${Components.statCard('Total Comments', summary.total_comments)}
                </div>

                ${Components.growthRateCards(summary.growth_rates)}

                ${Components.dateRangeBar(this._dateRange)}

                <div class="chart-container">
                    <h3>Views Over Time (Aggregate)</h3>
                    <div class="chart-wrap"><canvas id="chart-agg-views"></canvas></div>
                </div>

                <div class="chart-row">
                    <div class="chart-container">
                        <h3>Top Viewed</h3>
                        <div class="chart-wrap"><canvas id="chart-top-views"></canvas></div>
                    </div>
                    <div class="chart-container">
                        <h3>Top Faved</h3>
                        <div class="chart-wrap"><canvas id="chart-top-faves"></canvas></div>
                    </div>
                </div>

                <div class="chart-row">
                    <div class="chart-container" style="grid-column: 1 / -1">
                        <h3>Fastest Growing (24h)</h3>
                        ${Components.wsTopList(summary.fastest_growing, 'views_gained')}
                    </div>
                </div>
            `;

            this._setContent(html);

            // Conditionally render each chart only when data exists
            if (agg.snapshots && agg.snapshots.length > 0) {
                Charts.aggregateLine('chart-agg-views', agg.snapshots, ['views']);
            }
            if (summary.top_viewed && summary.top_viewed.length > 0) {
                Charts.topBar('chart-top-views', summary.top_viewed, 'views');
            }
            if (summary.top_faved && summary.top_faved.length > 0) {
                Charts.topBar('chart-top-faves', summary.top_faved, 'favorites_count');
            }

            this._loadFollowerWidget('ws', this._acctId('ws'));
            this._bindDateRange(() => this.renderWSDashboard());
            this._bindPinAndGoalActions(() => this.renderWSDashboard());
            this._startAutoRefresh(() => this.renderWSDashboard());
        } catch (err) {
            this._setContent(`<div class="empty-state"><h3>Error loading WS dashboard</h3><p>${Utils.escapeHtml(err.message)}</p></div>`);
        }
    },

    // ── WS Submissions ────────────────────────────────────────
    // Weasyl submissions table. Identical structure to FA submissions but with
    // different rating options: General/Mature/Explicit (Weasyl terminology)
    // instead of General/Mature/Adult (FA terminology). Sort and search follow
    // the same pattern using _wsSortState and _bindWSSearch/_bindWSTableSort.

    async renderWSSubmissions() {
        this._loading();
        try {
            // Fetch WS submissions with current sort column and direction
            const data = await API.getWSSubmissions({
                sort_by: this._wsSortState.field,
                order: this._wsSortState.order,
                account_id: this._acctId('ws'),
            });

            const _vm = localStorage.getItem('pp-view-mode') || 'grid';
            // 2.16.14 (BUG-021): closure so the search filter can re-render
            const wsGridRenderer = (subs) => Components.submissionCardGrid(subs, {
                idKey: 'submission_id', titleKey: 'title', thumbKey: 'thumbnail_url',
                detailRoute: '/ws/submission', dateKey: 'posted_at', proxyThumb: false,
                stats: [
                    { key: 'views', deltaKey: 'views_delta', label: 'views' },
                    { key: 'favorites_count', deltaKey: 'faves_delta', label: 'faves' },
                    { key: 'comments_count', deltaKey: 'comments_delta', label: 'comments' },
                ],
            });
            const gridHtml = wsGridRenderer(data.submissions);
            const html = `
                ${this._refreshIndicatorHtml()}
                <div class="page-header"><h2>Weasyl Submissions</h2></div>
                <div class="toolbar">
                    <input type="text" class="search-input" id="search-input" placeholder="Search titles and keywords...">
                    <select class="filter-select" id="filter-rating">
                        <option value="">All Ratings</option>
                        <option value="General">General</option>
                        <option value="Mature">Mature</option>
                        <option value="Explicit">Explicit</option>
                    </select>
                    <div class="view-toggle">
                        <button class="view-toggle-btn ${_vm === 'grid' ? 'active' : ''}" data-view="grid" title="Grid view">&#9638;</button>
                        <button class="view-toggle-btn ${_vm === 'list' ? 'active' : ''}" data-view="list" title="List view">&#9776;</button>
                    </div>
                </div>
                <div id="grid-container" style="${_vm !== 'grid' ? 'display:none' : ''}">${gridHtml}</div>
                <div id="table-container" class="table-scroll" style="${_vm !== 'list' ? 'display:none' : ''}">
                    ${Components.wsSubmissionsTable(data.submissions)}
                </div>
            `;

            this._setContent(html);
            this._bindViewToggle();
            this._bindWSTableSort();
            this._bindWSSearch(data.submissions, wsGridRenderer);
            this._startAutoRefresh(() => this.renderWSSubmissions());
        } catch (err) {
            this._setContent(`<div class="empty-state"><h3>Error loading WS submissions</h3><p>${Utils.escapeHtml(err.message)}</p></div>`);
        }
    },

    // ── WS Submission Detail ──────────────────────────────────
    // Weasyl submission detail page. Simpler than FA and IB detail pages:
    // no comments section (WS API doesn't expose comment text) and no
    // faving-users list. Shows thumbnail, title, author, date, rating,
    // external link to Weasyl, stat counters, keywords, growth rate cards,
    // and a stats-over-time chart.

    async renderWSDetail(id) {
        this._loading();
        try {
            const [data, pins, allTags] = await Promise.all([
                API.getWSSubmission(id),
                API.getPins().catch(() => ({ pins: [] })),
                API.getTags().catch(() => ({ tags: [] })),
            ]);
            const sub = data.submission;
            const isPinned = (pins.pins || []).some(p => p.platform === 'ws' && p.submission_id === id);
            const currentTags = sub.tags || [];

            const html = `
                ${this._refreshIndicatorHtml()}
                <a href="#/ws/submissions" class="back-link">&larr; Back to WS Submissions</a>
                <div class="detail-header">
                    ${sub.thumbnail_url ? `<img src="${Utils.escapeHtml(sub.thumbnail_url)}" class="detail-thumb">` : ''}
                    <div class="detail-info">
                        <h2>${Utils.escapeHtml(sub.title)}</h2>
                        <div class="detail-meta">by ${Utils.escapeHtml(sub.username)} &middot; ${Utils.formatDate(sub.posted_at)} &middot; ${Utils.escapeHtml(sub.rating || '')}</div>
                        <div class="detail-meta"><a href="${Utils.escapeHtml(Utils.safeUrl(sub.link) || '#')}" target="_blank">View on Weasyl</a></div>
                        <div class="detail-stats">
                            <div class="detail-stat">${Utils.formatNumber(sub.views)} <span class="lbl">views</span></div>
                            <div class="detail-stat">${Utils.formatNumber(sub.favorites_count)} <span class="lbl">faves</span></div>
                            <div class="detail-stat">${Utils.formatNumber(sub.comments_count)} <span class="lbl">comments</span></div>
                        </div>
                        <div style="margin-top:8px;display:flex;gap:8px;align-items:center;flex-wrap:wrap">
                            <button class="btn ${isPinned ? 'btn-danger' : 'btn-secondary'} btn-pin" data-platform="ws" data-id="${id}" style="padding:4px 10px;font-size:12px">${isPinned ? 'Unpin' : 'Pin'}</button>
                            ${currentTags.map(t => Components.tagBadge(t)).join('')}
                            <button class="btn btn-secondary btn-add-tag" data-platform="ws" data-id="${id}" style="padding:4px 10px;font-size:12px">+ Tag</button>
                        </div>
                        <div style="margin-top:8px">${Components.keywords(sub.keywords)}</div>
                    </div>
                </div>

                ${Components.growthRateCards(data.growth_rates)}

                ${Components.dateRangeBar(this._dateRange)}

                <div class="chart-container">
                    <h3>Stats Over Time</h3>
                    <div class="chart-wrap"><canvas id="chart-detail"></canvas></div>
                </div>
            `;

            this._setContent(html);

            // Render stats-over-time chart if snapshot data exists
            if (data.snapshots && data.snapshots.length > 0) {
                Charts.submissionLine('chart-detail', data.snapshots);
            }

            // Date range changes re-fetch only WS snapshots and redraw chart
            this._bindDateRange(async () => {
                const range = Utils.getDateRange(this._dateRange);
                const snaps = await API.getWSSnapshots(id, range);
                Charts.submissionLine('chart-detail', snaps.snapshots);
            });

            this._bindDetailPinTag('ws', id, allTags.tags || [], () => this.renderWSDetail(id));
            this._startAutoRefresh(() => this.renderWSDetail(id));
        } catch (err) {
            this._setContent(`<div class="empty-state"><h3>Error loading WS submission</h3><p>${Utils.escapeHtml(err.message)}</p></div>`);
        }
    },

    // ── WS Compare ────────────────────────────────────────────
    // Weasyl comparison page. Same chip/metric/chart pattern as the IB and FA
    // comparison pages. Selected IDs in _wsCompareIds, metric in _wsCompareMetric.

    async renderWSCompare() {
        this._loading();
        try {
            // Fetch all WS submissions sorted by views for chip population
            const data = await API.getWSSubmissions({ sort_by: 'views', order: 'desc', account_id: this._acctId('ws') });
            const subs = data.submissions;

            // Build toggle chips for each submission with pre-selected state
            const chips = subs.map(s => `
                <label class="compare-chip ${this._wsCompareIds.has(s.submission_id) ? 'selected' : ''}" data-id="${s.submission_id}">
                    <input type="checkbox" ${this._wsCompareIds.has(s.submission_id) ? 'checked' : ''}>
                    ${Utils.escapeHtml(Utils.truncate(s.title, 25))}
                </label>
            `).join('');

            const html = `
                ${this._refreshIndicatorHtml()}
                <div class="page-header">
                    <h2>Compare WS Submissions</h2>
                    <div>
                        <select class="filter-select" id="compare-metric">
                            <option value="views" ${this._wsCompareMetric === 'views' ? 'selected' : ''}>Views</option>
                            <option value="favorites_count" ${this._wsCompareMetric === 'favorites_count' ? 'selected' : ''}>Favourites</option>
                            <option value="comments_count" ${this._wsCompareMetric === 'comments_count' ? 'selected' : ''}>Comments</option>
                        </select>
                    </div>
                </div>
                <p style="font-size:13px;color:var(--text-muted);margin-bottom:12px">Select 2-5 Weasyl submissions to compare their trends over time.</p>
                <div class="compare-select">${chips}</div>

                ${Components.dateRangeBar(this._dateRange)}

                <div class="chart-container" id="compare-chart-container" style="${this._wsCompareIds.size < 2 ? 'display:none' : ''}">
                    <h3>Comparison</h3>
                    <div class="chart-wrap"><canvas id="chart-compare"></canvas></div>
                </div>
                ${this._wsCompareIds.size < 2 ? '<div class="empty-state"><p>Select at least 2 submissions above to see their trends compared.</p></div>' : ''}
            `;

            this._setContent(html);

            // Chip click handlers: toggle selection in _wsCompareIds (max 5), re-render
            document.querySelectorAll('.compare-chip').forEach(chip => {
                chip.addEventListener('click', (e) => {
                    e.preventDefault();
                    const id = parseInt(chip.dataset.id);
                    if (this._wsCompareIds.has(id)) {
                        this._wsCompareIds.delete(id);
                    } else if (this._wsCompareIds.size < 5) {
                        this._wsCompareIds.add(id);
                    }
                    this.renderWSCompare();
                });
            });

            // Metric dropdown change reloads the comparison chart with the new metric
            const metricSelect = document.getElementById('compare-metric');
            if (metricSelect) {
                metricSelect.addEventListener('change', () => {
                    this._wsCompareMetric = metricSelect.value;
                    this._loadWSComparisonChart();
                });
            }

            // Date range changes also reload the comparison chart
            this._bindDateRange(() => this._loadWSComparisonChart());

            // If 2+ submissions already selected, load chart immediately
            if (this._wsCompareIds.size >= 2) {
                await this._loadWSComparisonChart();
            }

            this._startAutoRefresh(() => this.renderWSCompare());
        } catch (err) {
            this._setContent(`<div class="empty-state"><h3>Error</h3><p>${Utils.escapeHtml(err.message)}</p></div>`);
        }
    },

    // Fetches WS comparison data for selected submissions and renders the
    // multi-line comparison chart. Unhides the chart container if it was hidden.
    async _loadWSComparisonChart() {
        try {
            if (this._wsCompareIds.size < 2) return;  // Guard: need 2+ selections
            const range = Utils.getDateRange(this._dateRange);
            const data = await API.getWSComparison([...this._wsCompareIds], range);
            const container = document.getElementById('compare-chart-container');
            if (container) container.style.display = '';  // Unhide if previously hidden
            Charts.comparisonLine('chart-compare', data.series, data.titles, this._wsCompareMetric);
        } catch (e) {
            console.error('Failed to load WS comparison chart:', e);
        }
    },

    // ── SF Dashboard ──────────────────────────────────────────
    // SoFurry-specific dashboard. Same layout as WS dashboard.

    async renderSFDashboard() {
        this._loading();
        try {
            const [summary, agg, pins, goals] = await Promise.all([
                API.getSFSummary({ account_id: this._acctId('sf') }),
                API.getSFAggregate({ ...Utils.getDateRange(this._dateRange), account_id: this._acctId('sf') }),
                API.getPins().catch(() => ({ pins: [] })),
                API.getGoals().catch(() => ({ goals: [] })),
            ]);
            const sfPins = (pins.pins || []).filter(p => p.platform === 'sf');
            const sfGoals = (goals.goals || []).filter(g => g.platform === 'sf' || g.platform === 'all');

            // Empty-state short-circuit: if SoFurry hasn't been
            // configured (or has zero submissions polled yet) show
            // the friendly connect CTA instead of empty cards.
            const sfHealth = window.PlatformHealth && window.PlatformHealth.get('sf');
            const isUnconfigured = sfHealth && sfHealth.configured === false;
            if (isUnconfigured || (summary.total_submissions || 0) === 0) {
                this._setContent(`
                    ${this._refreshIndicatorHtml()}
                    <div class="page-header">
                        <h2>SoFurry Dashboard</h2>
                    </div>
                    ${Components.platformEmptyState('sf', isUnconfigured
                        ? {}
                        : { reason: 'SoFurry is configured but no submissions have been polled yet. The first poll may still be running — give it a minute.' })}
                `);
                return;
            }

            const html = `
                ${this._refreshIndicatorHtml()}
                <div class="page-header">
                    <h2>SoFurry Dashboard</h2>
                    <div style="display:flex;gap:8px">
                        <button class="btn btn-primary" data-poll="sf">Poll Now</button>
                        <button class="btn btn-secondary" data-resync="sf">Full Resync</button>
                        <button class="btn btn-secondary" data-export="sf">Export CSV</button>
                    </div>
                </div>

                ${sfPins.length ? Components.pinnedSubmissions(sfPins, 'sf') : ''}
                ${sfGoals.length ? `<div class="goals-section"><h3>Goals</h3>${Components.goalProgressCards(sfGoals)}</div>` : ''}

                <div class="stats-grid">
                    ${Components.statCard('Total Submissions', summary.total_submissions, null, '#/sf/submissions')}
                    ${Components.statCard('Total Views', summary.total_views)}
                    ${Components.statCard('Total Likes', summary.total_favorites)}
                    ${Components.statCard('Total Comments', summary.total_comments)}
                    ${Components.statCard('Total Followers', summary.total_watchers || 0)}
                </div>

                ${Components.growthRateCards(summary.growth_rates)}

                ${Components.dateRangeBar(this._dateRange)}

                <div class="chart-container">
                    <h3>Views Over Time (Aggregate)</h3>
                    <div class="chart-wrap"><canvas id="chart-agg-views"></canvas></div>
                </div>

                <div class="chart-row">
                    <div class="chart-container">
                        <h3>Top Viewed</h3>
                        <div class="chart-wrap"><canvas id="chart-top-views"></canvas></div>
                    </div>
                    <div class="chart-container">
                        <h3>Top Liked</h3>
                        <div class="chart-wrap"><canvas id="chart-top-faves"></canvas></div>
                    </div>
                </div>

                <div class="chart-row">
                    <div class="chart-container">
                        <h3>Fastest Growing (24h)</h3>
                        ${Components.sfTopList(summary.fastest_growing, 'views_gained')}
                    </div>
                    <div class="chart-container">
                        <h3>Recent Followers</h3>
                        ${Components.recentWatchers(summary.recent_watchers)}
                    </div>
                </div>
            `;

            this._setContent(html);

            if (agg.snapshots && agg.snapshots.length > 0) {
                Charts.aggregateLine('chart-agg-views', agg.snapshots, ['views']);
            }
            if (summary.top_viewed && summary.top_viewed.length > 0) {
                Charts.topBar('chart-top-views', summary.top_viewed, 'views');
            }
            if (summary.top_faved && summary.top_faved.length > 0) {
                Charts.topBar('chart-top-faves', summary.top_faved, 'favorites_count');
            }

            this._bindDateRange(() => this.renderSFDashboard());
            this._bindPinAndGoalActions(() => this.renderSFDashboard());
            this._startAutoRefresh(() => this.renderSFDashboard());
        } catch (err) {
            this._setContent(`<div class="empty-state"><h3>Error loading SF dashboard</h3><p>${Utils.escapeHtml(err.message)}</p></div>`);
        }
    },

    // ── SF Submissions ────────────────────────────────────────

    async renderSFSubmissions() {
        this._loading();
        try {
            const data = await API.getSFSubmissions({
                sort_by: this._sfSortState.field,
                order: this._sfSortState.order,
                account_id: this._acctId('sf'),
            });

            const _vm = localStorage.getItem('pp-view-mode') || 'grid';
            // 2.16.14 (BUG-021): closure so the search filter can re-render
            const sfGridRenderer = (subs) => Components.submissionCardGrid(subs, {
                idKey: 'submission_id', titleKey: 'title', thumbKey: 'thumbnail_url',
                detailRoute: '/sf/submission', dateKey: 'posted_at', proxyThumb: false,
                stats: [
                    { key: 'views', deltaKey: 'views_delta', label: 'views' },
                    { key: 'favorites_count', deltaKey: 'faves_delta', label: 'likes' },
                    { key: 'comments_count', deltaKey: 'comments_delta', label: 'comments' },
                ],
            });
            const gridHtml = sfGridRenderer(data.submissions);
            const html = `
                ${this._refreshIndicatorHtml()}
                <div class="page-header"><h2>SoFurry Submissions</h2></div>
                <div class="toolbar">
                    <input type="text" class="search-input" id="search-input" placeholder="Search titles and keywords...">
                    <select class="filter-select" id="filter-rating">
                        <option value="">All Ratings</option>
                        <option value="Clean">Clean</option>
                        <option value="Mature">Mature</option>
                        <option value="Adult">Adult</option>
                    </select>
                    <div class="view-toggle">
                        <button class="view-toggle-btn ${_vm === 'grid' ? 'active' : ''}" data-view="grid" title="Grid view">&#9638;</button>
                        <button class="view-toggle-btn ${_vm === 'list' ? 'active' : ''}" data-view="list" title="List view">&#9776;</button>
                    </div>
                </div>
                <div id="grid-container" style="${_vm !== 'grid' ? 'display:none' : ''}">${gridHtml}</div>
                <div id="table-container" class="table-scroll" style="${_vm !== 'list' ? 'display:none' : ''}">
                    ${Components.sfSubmissionsTable(data.submissions)}
                </div>
            `;

            this._setContent(html);
            this._bindViewToggle();
            this._bindSFTableSort();
            this._bindSFSearch(data.submissions, sfGridRenderer);
            this._startAutoRefresh(() => this.renderSFSubmissions());
        } catch (err) {
            this._setContent(`<div class="empty-state"><h3>Error loading SF submissions</h3><p>${Utils.escapeHtml(err.message)}</p></div>`);
        }
    },

    // ── SF Submission Detail ──────────────────────────────────

    async renderSFDetail(id) {
        this._loading();
        try {
            const [data, pins, allTags] = await Promise.all([
                API.getSFSubmission(id),
                API.getPins().catch(() => ({ pins: [] })),
                API.getTags().catch(() => ({ tags: [] })),
            ]);
            const sub = data.submission;
            const isPinned = (pins.pins || []).some(p => p.platform === 'sf' && String(p.submission_id) === String(id));
            const currentTags = sub.tags || [];

            const html = `
                ${this._refreshIndicatorHtml()}
                <a href="#/sf/submissions" class="back-link">&larr; Back to SF Submissions</a>
                <div class="detail-header">
                    ${sub.thumbnail_url ? `<img src="${Utils.escapeHtml(sub.thumbnail_url)}" class="detail-thumb">` : ''}
                    <div class="detail-info">
                        <h2>${Utils.escapeHtml(sub.title)}</h2>
                        <div class="detail-meta">by ${Utils.escapeHtml(sub.username)} &middot; ${Utils.escapeHtml(sub.posted_at || '')} &middot; ${Utils.escapeHtml(sub.rating || '')}</div>
                        <div class="detail-meta"><a href="${Utils.escapeHtml(Utils.safeUrl(sub.link) || '#')}" target="_blank">View on SoFurry</a></div>
                        <div class="detail-stats">
                            <div class="detail-stat">${Utils.formatNumber(sub.views)} <span class="lbl">views</span></div>
                            <div class="detail-stat">${Utils.formatNumber(sub.favorites_count)} <span class="lbl">likes</span></div>
                            <div class="detail-stat">${Utils.formatNumber(sub.comments_count)} <span class="lbl">comments</span></div>
                        </div>
                        <div style="margin-top:8px;display:flex;gap:8px;align-items:center;flex-wrap:wrap">
                            <button class="btn ${isPinned ? 'btn-danger' : 'btn-secondary'} btn-pin" data-platform="sf" data-id="${id}" style="padding:4px 10px;font-size:12px">${isPinned ? 'Unpin' : 'Pin'}</button>
                            ${currentTags.map(t => Components.tagBadge(t)).join('')}
                            <button class="btn btn-secondary btn-add-tag" data-platform="sf" data-id="${id}" style="padding:4px 10px;font-size:12px">+ Tag</button>
                        </div>
                        <div style="margin-top:8px">${Components.keywords(sub.keywords)}</div>
                    </div>
                </div>

                ${Components.growthRateCards(data.growth_rates)}

                ${Components.dateRangeBar(this._dateRange)}

                <div class="chart-container">
                    <h3>Stats Over Time</h3>
                    <div class="chart-wrap"><canvas id="chart-detail"></canvas></div>
                </div>
            `;

            this._setContent(html);

            if (data.snapshots && data.snapshots.length > 0) {
                Charts.submissionLine('chart-detail', data.snapshots);
            }

            this._bindDateRange(async () => {
                const range = Utils.getDateRange(this._dateRange);
                const snaps = await API.getSFSnapshots(id, range);
                Charts.submissionLine('chart-detail', snaps.snapshots);
            });

            this._bindDetailPinTag('sf', id, allTags.tags || [], () => this.renderSFDetail(id));
            this._startAutoRefresh(() => this.renderSFDetail(id));
        } catch (err) {
            this._setContent(`<div class="empty-state"><h3>Error loading SF submission</h3><p>${Utils.escapeHtml(err.message)}</p></div>`);
        }
    },

    // ── SF Compare ────────────────────────────────────────────

    async renderSFCompare() {
        this._loading();
        try {
            const data = await API.getSFSubmissions({ sort_by: 'views', order: 'desc', account_id: this._acctId('sf') });
            const subs = data.submissions;

            const chips = subs.map(s => `
                <label class="compare-chip ${this._sfCompareIds.has(s.submission_id) ? 'selected' : ''}" data-id="${s.submission_id}">
                    <input type="checkbox" ${this._sfCompareIds.has(s.submission_id) ? 'checked' : ''}>
                    ${Utils.escapeHtml(Utils.truncate(s.title, 25))}
                </label>
            `).join('');

            const html = `
                ${this._refreshIndicatorHtml()}
                <div class="page-header">
                    <h2>Compare SF Submissions</h2>
                    <div>
                        <select class="filter-select" id="compare-metric">
                            <option value="views" ${this._sfCompareMetric === 'views' ? 'selected' : ''}>Views</option>
                            <option value="favorites_count" ${this._sfCompareMetric === 'favorites_count' ? 'selected' : ''}>Likes</option>
                            <option value="comments_count" ${this._sfCompareMetric === 'comments_count' ? 'selected' : ''}>Comments</option>
                        </select>
                    </div>
                </div>
                <p style="font-size:13px;color:var(--text-muted);margin-bottom:12px">Select 2-5 SoFurry submissions to compare their trends over time.</p>
                <div class="compare-select">${chips}</div>

                ${Components.dateRangeBar(this._dateRange)}

                <div class="chart-container" id="compare-chart-container" style="${this._sfCompareIds.size < 2 ? 'display:none' : ''}">
                    <h3>Comparison</h3>
                    <div class="chart-wrap"><canvas id="chart-compare"></canvas></div>
                </div>
                ${this._sfCompareIds.size < 2 ? '<div class="empty-state"><p>Select at least 2 submissions above to see their trends compared.</p></div>' : ''}
            `;

            this._setContent(html);

            document.querySelectorAll('.compare-chip').forEach(chip => {
                chip.addEventListener('click', (e) => {
                    e.preventDefault();
                    const id = parseInt(chip.dataset.id);
                    if (this._sfCompareIds.has(id)) {
                        this._sfCompareIds.delete(id);
                    } else if (this._sfCompareIds.size < 5) {
                        this._sfCompareIds.add(id);
                    }
                    this.renderSFCompare();
                });
            });

            const metricSelect = document.getElementById('compare-metric');
            if (metricSelect) {
                metricSelect.addEventListener('change', () => {
                    this._sfCompareMetric = metricSelect.value;
                    this._loadSFComparisonChart();
                });
            }

            this._bindDateRange(() => this._loadSFComparisonChart());

            if (this._sfCompareIds.size >= 2) {
                await this._loadSFComparisonChart();
            }

            this._startAutoRefresh(() => this.renderSFCompare());
        } catch (err) {
            this._setContent(`<div class="empty-state"><h3>Error</h3><p>${Utils.escapeHtml(err.message)}</p></div>`);
        }
    },

    async _loadSFComparisonChart() {
        try {
            if (this._sfCompareIds.size < 2) return;
            const range = Utils.getDateRange(this._dateRange);
            const data = await API.getSFComparison([...this._sfCompareIds], range);
            const container = document.getElementById('compare-chart-container');
            if (container) container.style.display = '';
            Charts.comparisonLine('chart-compare', data.series, data.titles, this._sfCompareMetric);
        } catch (e) {
            console.error('Failed to load SF comparison chart:', e);
        }
    },

    // ── SQW Dashboard ──────────────────────────────────────────

    async renderSQWDashboard() {
        this._loading();
        try {
            const [summary, agg, pins, goals] = await Promise.all([
                API.getSQWSummary({ account_id: this._acctId('sqw') }),
                API.getSQWAggregate({ ...Utils.getDateRange(this._dateRange), account_id: this._acctId('sqw') }),
                API.getPins().catch(() => ({ pins: [] })),
                API.getGoals().catch(() => ({ goals: [] })),
            ]);
            const sqwPins = (pins.pins || []).filter(p => p.platform === 'sqw');
            const sqwGoals = (goals.goals || []).filter(g => g.platform === 'sqw' || g.platform === 'all');

            // Empty-state short-circuit: see SF dashboard for the pattern.
            const sqwHealth = window.PlatformHealth && window.PlatformHealth.get('sqw');
            const isUnconfigured = sqwHealth && sqwHealth.configured === false;
            if (isUnconfigured || (summary.total_submissions || 0) === 0) {
                this._setContent(`
                    ${this._refreshIndicatorHtml()}
                    <div class="page-header"><h2>SquidgeWorld Dashboard</h2></div>
                    ${Components.platformEmptyState('sqw', isUnconfigured ? {} : { reason: 'SquidgeWorld is configured but no works have been polled yet. The first poll may still be running.' })}
                `);
                return;
            }

            const html = `
                ${this._refreshIndicatorHtml()}
                <div class="page-header">
                    <h2>SquidgeWorld Dashboard</h2>
                    <div style="display:flex;gap:8px">
                        <button class="btn btn-primary" data-poll="sqw">Poll Now</button>
                        <button class="btn btn-secondary" data-resync="sqw">Full Resync</button>
                        <button class="btn btn-secondary" data-export="sqw">Export CSV</button>
                    </div>
                </div>

                ${sqwPins.length ? Components.pinnedSubmissions(sqwPins, 'sqw') : ''}
                ${sqwGoals.length ? `<div class="goals-section"><h3>Goals</h3>${Components.goalProgressCards(sqwGoals)}</div>` : ''}

                <div class="stats-grid">
                    ${Components.statCard('Total Works', summary.total_submissions, null, '#/sqw/submissions')}
                    ${Components.statCard('Total Hits', summary.total_views)}
                    ${Components.statCard('Total Kudos', summary.total_favorites)}
                    ${Components.statCard('Total Comments', summary.total_comments)}
                </div>

                ${Components.growthRateCards(summary.growth_rates)}

                ${Components.dateRangeBar(this._dateRange)}

                <div class="chart-container">
                    <h3>Hits Over Time (Aggregate)</h3>
                    <div class="chart-wrap"><canvas id="chart-agg-views"></canvas></div>
                </div>

                <div class="chart-row">
                    <div class="chart-container">
                        <h3>Top Viewed</h3>
                        <div class="chart-wrap"><canvas id="chart-top-views"></canvas></div>
                    </div>
                    <div class="chart-container">
                        <h3>Most Kudos</h3>
                        <div class="chart-wrap"><canvas id="chart-top-faves"></canvas></div>
                    </div>
                </div>

                <div class="chart-row">
                    <div class="chart-container" style="grid-column: 1 / -1">
                        <h3>Fastest Growing (24h)</h3>
                        ${Components.sqwTopList(summary.fastest_growing, 'views_gained')}
                    </div>
                </div>
            `;

            this._setContent(html);

            if (agg.snapshots && agg.snapshots.length > 0) {
                Charts.aggregateLine('chart-agg-views', agg.snapshots, ['views']);
            }
            if (summary.top_viewed && summary.top_viewed.length > 0) {
                Charts.topBar('chart-top-views', summary.top_viewed, 'views');
            }
            if (summary.top_faved && summary.top_faved.length > 0) {
                Charts.topBar('chart-top-faves', summary.top_faved, 'favorites_count');
            }

            this._bindDateRange(() => this.renderSQWDashboard());
            this._bindPinAndGoalActions(() => this.renderSQWDashboard());
            this._startAutoRefresh(() => this.renderSQWDashboard());
        } catch (err) {
            this._setContent(`<div class="empty-state"><h3>Error loading SqW dashboard</h3><p>${Utils.escapeHtml(err.message)}</p></div>`);
        }
    },

    // ── SQW Submissions ────────────────────────────────────────

    async renderSQWSubmissions() {
        this._loading();
        try {
            const data = await API.getSQWSubmissions({
                sort_by: this._sqwSortState.field,
                order: this._sqwSortState.order,
                account_id: this._acctId('sqw'),
            });

            const _vm = localStorage.getItem('pp-view-mode') || 'grid';
            // 2.16.14 (BUG-021): closure so the search filter can re-render
            const sqwGridRenderer = (subs) => Components.submissionCardGrid(subs, {
                idKey: 'submission_id', titleKey: 'title', thumbKey: null,
                detailRoute: '/sqw/submission', dateKey: 'posted_at',
                stats: [
                    { key: 'views', deltaKey: 'views_delta', label: 'hits' },
                    { key: 'favorites_count', deltaKey: 'faves_delta', label: 'kudos' },
                    { key: 'comments_count', deltaKey: 'comments_delta', label: 'comments' },
                ],
            });
            const gridHtml = sqwGridRenderer(data.submissions);
            const html = `
                ${this._refreshIndicatorHtml()}
                <div class="page-header"><h2>SquidgeWorld Works</h2></div>
                <div class="toolbar">
                    <input type="text" class="search-input" id="search-input" placeholder="Search titles and tags...">
                    <select class="filter-select" id="filter-rating">
                        <option value="">All Ratings</option>
                        <option value="General Audiences">General</option>
                        <option value="Teen And Up Audiences">Teen</option>
                        <option value="Mature">Mature</option>
                        <option value="Explicit">Explicit</option>
                    </select>
                    <div class="view-toggle">
                        <button class="view-toggle-btn ${_vm === 'grid' ? 'active' : ''}" data-view="grid" title="Grid view">&#9638;</button>
                        <button class="view-toggle-btn ${_vm === 'list' ? 'active' : ''}" data-view="list" title="List view">&#9776;</button>
                    </div>
                </div>
                <div id="grid-container" style="${_vm !== 'grid' ? 'display:none' : ''}">${gridHtml}</div>
                <div id="table-container" class="table-scroll" style="${_vm !== 'list' ? 'display:none' : ''}">
                    ${Components.sqwSubmissionsTable(data.submissions)}
                </div>
            `;

            this._setContent(html);
            this._bindViewToggle();
            this._bindSQWTableSort();
            this._bindSQWSearch(data.submissions, sqwGridRenderer);
            this._startAutoRefresh(() => this.renderSQWSubmissions());
        } catch (err) {
            this._setContent(`<div class="empty-state"><h3>Error loading SqW submissions</h3><p>${Utils.escapeHtml(err.message)}</p></div>`);
        }
    },

    // ── SQW Submission Detail ──────────────────────────────────

    async renderSQWDetail(id) {
        this._loading();
        try {
            const [data, pins, allTags] = await Promise.all([
                API.getSQWSubmission(id),
                API.getPins().catch(() => ({ pins: [] })),
                API.getTags().catch(() => ({ tags: [] })),
            ]);
            const sub = data.submission;
            const isPinned = (pins.pins || []).some(p => p.platform === 'sqw' && String(p.submission_id) === String(id));
            const currentTags = sub.tags || [];

            const html = `
                ${this._refreshIndicatorHtml()}
                <a href="#/sqw/submissions" class="back-link">&larr; Back to SqW Works</a>
                <div class="detail-header">
                    <div class="detail-info">
                        <h2>${Utils.escapeHtml(sub.title)}</h2>
                        <div class="detail-meta">by ${Utils.escapeHtml(sub.username)} &middot; ${Utils.escapeHtml(sub.posted_at || '')} &middot; ${Utils.escapeHtml(sub.rating || '')}</div>
                        <div class="detail-meta"><a href="${Utils.escapeHtml(Utils.safeUrl(sub.link) || '#')}" target="_blank">View on SquidgeWorld</a></div>
                        <div class="detail-stats">
                            <div class="detail-stat">${Utils.formatNumber(sub.views)} <span class="lbl">hits</span></div>
                            <div class="detail-stat">${Utils.formatNumber(sub.favorites_count)} <span class="lbl">kudos</span></div>
                            <div class="detail-stat">${Utils.formatNumber(sub.comments_count)} <span class="lbl">comments</span></div>
                            <div class="detail-stat">${Utils.formatNumber(sub.bookmarks_count || 0)} <span class="lbl">bookmarks</span></div>
                        </div>
                        <div style="margin-top:8px;display:flex;gap:8px;align-items:center;flex-wrap:wrap">
                            <button class="btn ${isPinned ? 'btn-danger' : 'btn-secondary'} btn-pin" data-platform="sqw" data-id="${id}" style="padding:4px 10px;font-size:12px">${isPinned ? 'Unpin' : 'Pin'}</button>
                            ${currentTags.map(t => Components.tagBadge(t)).join('')}
                            <button class="btn btn-secondary btn-add-tag" data-platform="sqw" data-id="${id}" style="padding:4px 10px;font-size:12px">+ Tag</button>
                        </div>
                        <div style="margin-top:8px">${Components.keywords(sub.keywords)}</div>
                    </div>
                </div>

                ${Components.growthRateCards(data.growth_rates)}

                ${Components.dateRangeBar(this._dateRange)}

                <div class="chart-container">
                    <h3>Stats Over Time</h3>
                    <div class="chart-wrap"><canvas id="chart-detail"></canvas></div>
                </div>
            `;

            this._setContent(html);

            if (data.snapshots && data.snapshots.length > 0) {
                Charts.submissionLine('chart-detail', data.snapshots);
            }

            this._bindDateRange(async () => {
                const range = Utils.getDateRange(this._dateRange);
                const snaps = await API.getSQWSnapshots(id, range);
                Charts.submissionLine('chart-detail', snaps.snapshots);
            });

            this._bindDetailPinTag('sqw', id, allTags.tags || [], () => this.renderSQWDetail(id));
            this._startAutoRefresh(() => this.renderSQWDetail(id));
        } catch (err) {
            this._setContent(`<div class="empty-state"><h3>Error loading SqW work</h3><p>${Utils.escapeHtml(err.message)}</p></div>`);
        }
    },

    // ── SQW Compare ────────────────────────────────────────────

    async renderSQWCompare() {
        this._loading();
        try {
            const data = await API.getSQWSubmissions({ sort_by: 'views', order: 'desc', account_id: this._acctId('sqw') });
            const subs = data.submissions;

            const chips = subs.map(s => `
                <label class="compare-chip ${this._sqwCompareIds.has(s.submission_id) ? 'selected' : ''}" data-id="${s.submission_id}">
                    <input type="checkbox" ${this._sqwCompareIds.has(s.submission_id) ? 'checked' : ''}>
                    ${Utils.escapeHtml(Utils.truncate(s.title, 25))}
                </label>
            `).join('');

            const html = `
                ${this._refreshIndicatorHtml()}
                <div class="page-header">
                    <h2>Compare SqW Works</h2>
                    <div>
                        <select class="filter-select" id="compare-metric">
                            <option value="views" ${this._sqwCompareMetric === 'views' ? 'selected' : ''}>Hits</option>
                            <option value="favorites_count" ${this._sqwCompareMetric === 'favorites_count' ? 'selected' : ''}>Kudos</option>
                            <option value="comments_count" ${this._sqwCompareMetric === 'comments_count' ? 'selected' : ''}>Comments</option>
                        </select>
                    </div>
                </div>
                <p style="font-size:13px;color:var(--text-muted);margin-bottom:12px">Select 2-5 SquidgeWorld works to compare their trends over time.</p>
                <div class="compare-select">${chips}</div>

                ${Components.dateRangeBar(this._dateRange)}

                <div class="chart-container" id="compare-chart-container" style="${this._sqwCompareIds.size < 2 ? 'display:none' : ''}">
                    <h3>Comparison</h3>
                    <div class="chart-wrap"><canvas id="chart-compare"></canvas></div>
                </div>
                ${this._sqwCompareIds.size < 2 ? '<div class="empty-state"><p>Select at least 2 works above to see their trends compared.</p></div>' : ''}
            `;

            this._setContent(html);

            document.querySelectorAll('.compare-chip').forEach(chip => {
                chip.addEventListener('click', (e) => {
                    e.preventDefault();
                    const id = parseInt(chip.dataset.id);
                    if (this._sqwCompareIds.has(id)) {
                        this._sqwCompareIds.delete(id);
                    } else if (this._sqwCompareIds.size < 5) {
                        this._sqwCompareIds.add(id);
                    }
                    this.renderSQWCompare();
                });
            });

            const metricSelect = document.getElementById('compare-metric');
            if (metricSelect) {
                metricSelect.addEventListener('change', () => {
                    this._sqwCompareMetric = metricSelect.value;
                    this._loadSQWComparisonChart();
                });
            }

            this._bindDateRange(() => this._loadSQWComparisonChart());

            if (this._sqwCompareIds.size >= 2) {
                await this._loadSQWComparisonChart();
            }

            this._startAutoRefresh(() => this.renderSQWCompare());
        } catch (err) {
            this._setContent(`<div class="empty-state"><h3>Error</h3><p>${Utils.escapeHtml(err.message)}</p></div>`);
        }
    },

    async _loadSQWComparisonChart() {
        try {
            if (this._sqwCompareIds.size < 2) return;
            const range = Utils.getDateRange(this._dateRange);
            const data = await API.getSQWComparison([...this._sqwCompareIds], range);
            const container = document.getElementById('compare-chart-container');
            if (container) container.style.display = '';
            Charts.comparisonLine('chart-compare', data.series, data.titles, this._sqwCompareMetric);
        } catch (e) {
            console.error('Failed to load SqW comparison chart:', e);
        }
    },

    // ── AO3 Dashboard ──────────────────────────────────────────

    async renderAO3Dashboard() {
        this._loading();
        try {
            const [summary, agg, pins, goals] = await Promise.all([
                API.getAO3Summary({ account_id: this._acctId('ao3') }),
                API.getAO3Aggregate({ ...Utils.getDateRange(this._dateRange), account_id: this._acctId('ao3') }),
                API.getPins().catch(() => ({ pins: [] })),
                API.getGoals().catch(() => ({ goals: [] })),
            ]);
            const ao3Pins = (pins.pins || []).filter(p => p.platform === 'ao3');
            const ao3Goals = (goals.goals || []).filter(g => g.platform === 'ao3' || g.platform === 'all');

            // Empty-state short-circuit: see SF dashboard for the pattern.
            const ao3Health = window.PlatformHealth && window.PlatformHealth.get('ao3');
            const isUnconfigured = ao3Health && ao3Health.configured === false;
            if (isUnconfigured || (summary.total_submissions || 0) === 0) {
                this._setContent(`
                    ${this._refreshIndicatorHtml()}
                    <div class="page-header"><h2>AO3 Dashboard</h2></div>
                    ${Components.platformEmptyState('ao3', isUnconfigured ? {} : { reason: 'AO3 is configured but no works have been polled yet. The first poll may still be running.' })}
                `);
                return;
            }

            const html = `
                ${this._refreshIndicatorHtml()}
                <div class="page-header">
                    <h2>AO3 Dashboard</h2>
                    <div style="display:flex;gap:8px">
                        <button class="btn btn-primary" data-poll="ao3">Poll Now</button>
                        <button class="btn btn-secondary" data-resync="ao3">Full Resync</button>
                        <button class="btn btn-secondary" data-export="ao3">Export CSV</button>
                    </div>
                </div>

                ${ao3Pins.length ? Components.pinnedSubmissions(ao3Pins, 'ao3') : ''}
                ${ao3Goals.length ? `<div class="goals-section"><h3>Goals</h3>${Components.goalProgressCards(ao3Goals)}</div>` : ''}

                <div class="stats-grid">
                    ${Components.statCard('Total Works', summary.total_submissions, null, '#/ao3/submissions')}
                    ${Components.statCard('Total Hits', summary.total_views)}
                    ${Components.statCard('Total Kudos', summary.total_favorites)}
                    ${Components.statCard('Total Comments', summary.total_comments)}
                </div>

                ${Components.growthRateCards(summary.growth_rates)}

                ${Components.dateRangeBar(this._dateRange)}

                <div class="chart-container">
                    <h3>Hits Over Time (Aggregate)</h3>
                    <div class="chart-wrap"><canvas id="chart-agg-views"></canvas></div>
                </div>

                <div class="chart-row">
                    <div class="chart-container">
                        <h3>Top Viewed</h3>
                        <div class="chart-wrap"><canvas id="chart-top-views"></canvas></div>
                    </div>
                    <div class="chart-container">
                        <h3>Most Kudos</h3>
                        <div class="chart-wrap"><canvas id="chart-top-faves"></canvas></div>
                    </div>
                </div>

                <div class="chart-row">
                    <div class="chart-container" style="grid-column: 1 / -1">
                        <h3>Fastest Growing (24h)</h3>
                        ${Components.ao3TopList(summary.fastest_growing, 'views_gained')}
                    </div>
                </div>
            `;

            this._setContent(html);

            if (agg.snapshots && agg.snapshots.length > 0) {
                Charts.aggregateLine('chart-agg-views', agg.snapshots, ['views']);
            }
            if (summary.top_viewed && summary.top_viewed.length > 0) {
                Charts.topBar('chart-top-views', summary.top_viewed, 'views');
            }
            if (summary.top_faved && summary.top_faved.length > 0) {
                Charts.topBar('chart-top-faves', summary.top_faved, 'favorites_count');
            }

            this._bindDateRange(() => this.renderAO3Dashboard());
            this._bindPinAndGoalActions(() => this.renderAO3Dashboard());
            this._startAutoRefresh(() => this.renderAO3Dashboard());
        } catch (err) {
            this._setContent(`<div class="empty-state"><h3>Error loading AO3 dashboard</h3><p>${Utils.escapeHtml(err.message)}</p></div>`);
        }
    },

    // ── AO3 Submissions ────────────────────────────────────────

    async renderAO3Submissions() {
        this._loading();
        try {
            const data = await API.getAO3Submissions({
                sort_by: this._ao3SortState.field,
                order: this._ao3SortState.order,
                account_id: this._acctId('ao3'),
            });

            const _vm = localStorage.getItem('pp-view-mode') || 'grid';
            // 2.16.14 (BUG-021): closure so the search filter can re-render
            const ao3GridRenderer = (subs) => Components.submissionCardGrid(subs, {
                idKey: 'submission_id', titleKey: 'title', thumbKey: null,
                detailRoute: '/ao3/submission', dateKey: 'posted_at',
                stats: [
                    { key: 'views', deltaKey: 'views_delta', label: 'hits' },
                    { key: 'favorites_count', deltaKey: 'faves_delta', label: 'kudos' },
                    { key: 'comments_count', deltaKey: 'comments_delta', label: 'comments' },
                ],
            });
            const gridHtml = ao3GridRenderer(data.submissions);
            const html = `
                ${this._refreshIndicatorHtml()}
                <div class="page-header"><h2>AO3 Works</h2></div>
                <div class="toolbar">
                    <input type="text" class="search-input" id="search-input" placeholder="Search titles and tags...">
                    <select class="filter-select" id="filter-rating">
                        <option value="">All Ratings</option>
                        <option value="General Audiences">General</option>
                        <option value="Teen And Up Audiences">Teen</option>
                        <option value="Mature">Mature</option>
                        <option value="Explicit">Explicit</option>
                    </select>
                    <div class="view-toggle">
                        <button class="view-toggle-btn ${_vm === 'grid' ? 'active' : ''}" data-view="grid" title="Grid view">&#9638;</button>
                        <button class="view-toggle-btn ${_vm === 'list' ? 'active' : ''}" data-view="list" title="List view">&#9776;</button>
                    </div>
                </div>
                <div id="grid-container" style="${_vm !== 'grid' ? 'display:none' : ''}">${gridHtml}</div>
                <div id="table-container" class="table-scroll" style="${_vm !== 'list' ? 'display:none' : ''}">
                    ${Components.ao3SubmissionsTable(data.submissions)}
                </div>
            `;

            this._setContent(html);
            this._bindViewToggle();
            this._bindAO3TableSort();
            this._bindAO3Search(data.submissions, ao3GridRenderer);
            this._startAutoRefresh(() => this.renderAO3Submissions());
        } catch (err) {
            this._setContent(`<div class="empty-state"><h3>Error loading AO3 submissions</h3><p>${Utils.escapeHtml(err.message)}</p></div>`);
        }
    },

    // ── AO3 Submission Detail ──────────────────────────────────

    async renderAO3Detail(id) {
        this._loading();
        try {
            const [data, pins, allTags] = await Promise.all([
                API.getAO3Submission(id),
                API.getPins().catch(() => ({ pins: [] })),
                API.getTags().catch(() => ({ tags: [] })),
            ]);
            const sub = data.submission;
            const isPinned = (pins.pins || []).some(p => p.platform === 'ao3' && String(p.submission_id) === String(id));
            const currentTags = sub.tags || [];

            const html = `
                ${this._refreshIndicatorHtml()}
                <a href="#/ao3/submissions" class="back-link">&larr; Back to AO3 Works</a>
                <div class="detail-header">
                    <div class="detail-info">
                        <h2>${Utils.escapeHtml(sub.title)}</h2>
                        <div class="detail-meta">by ${Utils.escapeHtml(sub.username)} &middot; ${Utils.escapeHtml(sub.posted_at || '')} &middot; ${Utils.escapeHtml(sub.rating || '')}</div>
                        <div class="detail-meta"><a href="${Utils.escapeHtml(Utils.safeUrl(sub.link) || '#')}" target="_blank">View on AO3</a></div>
                        <div class="detail-stats">
                            <div class="detail-stat">${Utils.formatNumber(sub.views)} <span class="lbl">hits</span></div>
                            <div class="detail-stat">${Utils.formatNumber(sub.favorites_count)} <span class="lbl">kudos</span></div>
                            <div class="detail-stat">${Utils.formatNumber(sub.comments_count)} <span class="lbl">comments</span></div>
                            <div class="detail-stat">${Utils.formatNumber(sub.bookmarks_count || 0)} <span class="lbl">bookmarks</span></div>
                        </div>
                        <div style="margin-top:8px;display:flex;gap:8px;align-items:center;flex-wrap:wrap">
                            <button class="btn ${isPinned ? 'btn-danger' : 'btn-secondary'} btn-pin" data-platform="ao3" data-id="${id}" style="padding:4px 10px;font-size:12px">${isPinned ? 'Unpin' : 'Pin'}</button>
                            ${currentTags.map(t => Components.tagBadge(t)).join('')}
                            <button class="btn btn-secondary btn-add-tag" data-platform="ao3" data-id="${id}" style="padding:4px 10px;font-size:12px">+ Tag</button>
                        </div>
                        <div style="margin-top:8px">${Components.keywords(sub.keywords)}</div>
                    </div>
                </div>

                ${Components.growthRateCards(data.growth_rates)}

                ${Components.dateRangeBar(this._dateRange)}

                <div class="chart-container">
                    <h3>Stats Over Time</h3>
                    <div class="chart-wrap"><canvas id="chart-detail"></canvas></div>
                </div>
            `;

            this._setContent(html);

            if (data.snapshots && data.snapshots.length > 0) {
                Charts.submissionLine('chart-detail', data.snapshots);
            }

            this._bindDateRange(async () => {
                const range = Utils.getDateRange(this._dateRange);
                const snaps = await API.getAO3Snapshots(id, range);
                Charts.submissionLine('chart-detail', snaps.snapshots);
            });

            this._bindDetailPinTag('ao3', id, allTags.tags || [], () => this.renderAO3Detail(id));
            this._startAutoRefresh(() => this.renderAO3Detail(id));
        } catch (err) {
            this._setContent(`<div class="empty-state"><h3>Error loading AO3 work</h3><p>${Utils.escapeHtml(err.message)}</p></div>`);
        }
    },

    // ── AO3 Compare ────────────────────────────────────────────

    async renderAO3Compare() {
        this._loading();
        try {
            const data = await API.getAO3Submissions({ sort_by: 'views', order: 'desc', account_id: this._acctId('ao3') });
            const subs = data.submissions;

            const chips = subs.map(s => `
                <label class="compare-chip ${this._ao3CompareIds.has(s.submission_id) ? 'selected' : ''}" data-id="${s.submission_id}">
                    <input type="checkbox" ${this._ao3CompareIds.has(s.submission_id) ? 'checked' : ''}>
                    ${Utils.escapeHtml(Utils.truncate(s.title, 25))}
                </label>
            `).join('');

            const html = `
                ${this._refreshIndicatorHtml()}
                <div class="page-header">
                    <h2>Compare AO3 Works</h2>
                    <div>
                        <select class="filter-select" id="compare-metric">
                            <option value="views" ${this._ao3CompareMetric === 'views' ? 'selected' : ''}>Hits</option>
                            <option value="favorites_count" ${this._ao3CompareMetric === 'favorites_count' ? 'selected' : ''}>Kudos</option>
                            <option value="comments_count" ${this._ao3CompareMetric === 'comments_count' ? 'selected' : ''}>Comments</option>
                        </select>
                    </div>
                </div>
                <p style="font-size:13px;color:var(--text-muted);margin-bottom:12px">Select 2-5 AO3 works to compare their trends over time.</p>
                <div class="compare-select">${chips}</div>

                ${Components.dateRangeBar(this._dateRange)}

                <div class="chart-container" id="compare-chart-container" style="${this._ao3CompareIds.size < 2 ? 'display:none' : ''}">
                    <h3>Comparison</h3>
                    <div class="chart-wrap"><canvas id="chart-compare"></canvas></div>
                </div>
                ${this._ao3CompareIds.size < 2 ? '<div class="empty-state"><p>Select at least 2 works above to see their trends compared.</p></div>' : ''}
            `;

            this._setContent(html);

            document.querySelectorAll('.compare-chip').forEach(chip => {
                chip.addEventListener('click', (e) => {
                    e.preventDefault();
                    const id = parseInt(chip.dataset.id);
                    if (this._ao3CompareIds.has(id)) {
                        this._ao3CompareIds.delete(id);
                    } else if (this._ao3CompareIds.size < 5) {
                        this._ao3CompareIds.add(id);
                    }
                    this.renderAO3Compare();
                });
            });

            const metricSelect = document.getElementById('compare-metric');
            if (metricSelect) {
                metricSelect.addEventListener('change', () => {
                    this._ao3CompareMetric = metricSelect.value;
                    this._loadAO3ComparisonChart();
                });
            }

            this._bindDateRange(() => this._loadAO3ComparisonChart());

            if (this._ao3CompareIds.size >= 2) {
                await this._loadAO3ComparisonChart();
            }

            this._startAutoRefresh(() => this.renderAO3Compare());
        } catch (err) {
            this._setContent(`<div class="empty-state"><h3>Error</h3><p>${Utils.escapeHtml(err.message)}</p></div>`);
        }
    },

    async _loadAO3ComparisonChart() {
        try {
            if (this._ao3CompareIds.size < 2) return;
            const range = Utils.getDateRange(this._dateRange);
            const data = await API.getAO3Comparison([...this._ao3CompareIds], range);
            const container = document.getElementById('compare-chart-container');
            if (container) container.style.display = '';
            Charts.comparisonLine('chart-compare', data.series, data.titles, this._ao3CompareMetric);
        } catch (e) {
            console.error('Failed to load AO3 comparison chart:', e);
        }
    },

    // ── DA Dashboard ──────────────────────────────────────────
    // DeviantArt dashboard with stat cards including Downloads (unique to DA),
    // growth rates, top lists (top viewed, top faved, top downloaded), and poll log.

    async renderDADashboard() {
        this._loading();
        try {
            const [summary, agg, pins, goals] = await Promise.all([
                API.getDASummary({ account_id: this._acctId('da') }),
                API.getDAAggregate({ ...Utils.getDateRange(this._dateRange), account_id: this._acctId('da') }),
                API.getPins().catch(() => ({ pins: [] })),
                API.getGoals().catch(() => ({ goals: [] })),
            ]);
            const daPins = (pins.pins || []).filter(p => p.platform === 'da');
            const daGoals = (goals.goals || []).filter(g => g.platform === 'da' || g.platform === 'all');

            // Empty-state short-circuit: see SF dashboard for the pattern.
            const daHealth = window.PlatformHealth && window.PlatformHealth.get('da');
            const isUnconfigured = daHealth && daHealth.configured === false;
            if (isUnconfigured || (summary.total_submissions || 0) === 0) {
                this._setContent(`
                    ${this._refreshIndicatorHtml()}
                    <div class="page-header"><h2>DeviantArt Dashboard</h2></div>
                    ${Components.platformEmptyState('da', isUnconfigured ? {} : { reason: 'DeviantArt is configured but no deviations have been polled yet. The first poll may still be running.' })}
                `);
                return;
            }

            const html = `
                ${this._refreshIndicatorHtml()}
                <div class="page-header">
                    <h2>DeviantArt Dashboard</h2>
                    <div style="display:flex;gap:8px">
                        <button class="btn btn-primary" data-poll="da">Poll Now</button>
                        <button class="btn btn-secondary" data-resync="da">Full Resync</button>
                        <button class="btn btn-secondary" data-export="da">Export CSV</button>
                    </div>
                </div>

                ${daPins.length ? Components.pinnedSubmissions(daPins, 'da') : ''}
                ${daGoals.length ? `<div class="goals-section"><h3>Goals</h3>${Components.goalProgressCards(daGoals)}</div>` : ''}

                <div class="stats-grid">
                    ${Components.statCard('Total Submissions', summary.total_submissions, null, '#/da/submissions')}
                    ${Components.statCard('Total Views', summary.total_views)}
                    ${Components.statCard('Total Favourites', summary.total_favorites)}
                    ${Components.statCard('Total Comments', summary.total_comments)}
                    ${Components.statCard('Total Downloads', summary.total_downloads || 0)}
                </div>

                ${Components.growthRateCards(summary.growth_rates)}

                ${Components.dateRangeBar(this._dateRange)}

                <div class="chart-container">
                    <h3>Views Over Time (Aggregate)</h3>
                    <div class="chart-wrap"><canvas id="chart-agg-views"></canvas></div>
                </div>

                <div class="chart-row">
                    <div class="chart-container">
                        <h3>Top Viewed</h3>
                        <div class="chart-wrap"><canvas id="chart-top-views"></canvas></div>
                    </div>
                    <div class="chart-container">
                        <h3>Top Faved</h3>
                        <div class="chart-wrap"><canvas id="chart-top-faves"></canvas></div>
                    </div>
                </div>

                <div class="chart-row">
                    <div class="chart-container">
                        <h3>Top Downloaded</h3>
                        <div class="chart-wrap"><canvas id="chart-top-downloads"></canvas></div>
                    </div>
                    <div class="chart-container">
                        <h3>Fastest Growing (24h)</h3>
                        ${Components.daTopList(summary.fastest_growing, 'views_gained')}
                    </div>
                </div>
            `;

            this._setContent(html);

            if (agg.snapshots && agg.snapshots.length > 0) {
                Charts.aggregateLine('chart-agg-views', agg.snapshots, ['views']);
            }
            if (summary.top_viewed && summary.top_viewed.length > 0) {
                Charts.topBar('chart-top-views', summary.top_viewed, 'views');
            }
            if (summary.top_faved && summary.top_faved.length > 0) {
                Charts.topBar('chart-top-faves', summary.top_faved, 'favorites_count');
            }
            if (summary.top_downloaded && summary.top_downloaded.length > 0) {
                Charts.topBar('chart-top-downloads', summary.top_downloaded, 'downloads');
            }

            this._loadFollowerWidget('da', this._acctId('da'));
            this._bindDateRange(() => this.renderDADashboard());
            this._bindPinAndGoalActions(() => this.renderDADashboard());
            this._startAutoRefresh(() => this.renderDADashboard());
        } catch (err) {
            this._setContent(`<div class="empty-state"><h3>Error loading DA dashboard</h3><p>${Utils.escapeHtml(err.message)}</p></div>`);
        }
    },

    // ── DA Submissions ─────────────────────────────────────────
    // DA submissions table with text search (title/keywords) and rating filter.
    // Includes Downloads column unique to DeviantArt.

    async renderDASubmissions() {
        this._loading();
        try {
            const data = await API.getDASubmissions({
                sort_by: this._daSortState.field,
                order: this._daSortState.order,
                account_id: this._acctId('da'),
            });

            const _vm = localStorage.getItem('pp-view-mode') || 'grid';
            // 2.16.14 (BUG-021): closure so the search filter can re-render
            const daGridRenderer = (subs) => Components.submissionCardGrid(subs, {
                idKey: 'submission_id', titleKey: 'title', thumbKey: 'thumbnail_url', proxyThumb: false,
                detailRoute: '/da/submission', dateKey: 'posted_at',
                stats: [
                    { key: 'views', deltaKey: 'views_delta', label: 'views' },
                    { key: 'favorites_count', deltaKey: 'faves_delta', label: 'faves' },
                    { key: 'comments_count', deltaKey: 'comments_delta', label: 'comments' },
                ],
            });
            const gridHtml = daGridRenderer(data.submissions);
            const html = `
                ${this._refreshIndicatorHtml()}
                <div class="page-header"><h2>DA Submissions</h2></div>
                <div class="toolbar">
                    <input type="text" class="search-input" id="search-input" placeholder="Search titles and keywords...">
                    <select class="filter-select" id="filter-rating">
                        <option value="">All Ratings</option>
                        <option value="General">General</option>
                        <option value="Mature">Mature</option>
                    </select>
                    <div class="view-toggle">
                        <button class="view-toggle-btn ${_vm === 'grid' ? 'active' : ''}" data-view="grid" title="Grid view">&#9638;</button>
                        <button class="view-toggle-btn ${_vm === 'list' ? 'active' : ''}" data-view="list" title="List view">&#9776;</button>
                    </div>
                </div>
                <div id="grid-container" style="${_vm !== 'grid' ? 'display:none' : ''}">${gridHtml}</div>
                <div id="table-container" class="table-scroll" style="${_vm !== 'list' ? 'display:none' : ''}">
                    ${Components.daSubmissionsTable(data.submissions)}
                </div>
            `;

            this._setContent(html);
            this._bindViewToggle();
            this._bindDATableSort();
            this._bindDASearch(data.submissions, daGridRenderer);
            this._startAutoRefresh(() => this.renderDASubmissions());
        } catch (err) {
            this._setContent(`<div class="empty-state"><h3>Error loading DA submissions</h3><p>${Utils.escapeHtml(err.message)}</p></div>`);
        }
    },

    // ── DA Submission Detail ───────────────────────────────────
    // Individual DA submission detail page with 4 metrics including Downloads.

    async renderDADetail(id) {
        this._loading();
        try {
            const [data, pins, allTags] = await Promise.all([
                API.getDASubmission(id),
                API.getPins().catch(() => ({ pins: [] })),
                API.getTags().catch(() => ({ tags: [] })),
            ]);
            const sub = data.submission;
            const isPinned = (pins.pins || []).some(p => p.platform === 'da' && p.submission_id === id);
            const currentTags = sub.tags || [];

            const html = `
                ${this._refreshIndicatorHtml()}
                <a href="#/da/submissions" class="back-link">&larr; Back to DA Submissions</a>
                <div class="detail-header">
                    ${sub.thumbnail_url ? `<img src="${Utils.escapeHtml(sub.thumbnail_url)}" class="detail-thumb">` : ''}
                    <div class="detail-info">
                        <h2>${Utils.escapeHtml(sub.title)}</h2>
                        <div class="detail-meta">by ${Utils.escapeHtml(sub.username)} &middot; ${Utils.formatDate(sub.posted_at)} &middot; ${Utils.escapeHtml(sub.rating || '')}</div>
                        <div class="detail-meta"><a href="${Utils.escapeHtml(Utils.safeUrl(sub.link) || '#')}" target="_blank">View on DeviantArt</a></div>
                        <div class="detail-stats">
                            <div class="detail-stat">${Utils.formatNumber(sub.views)} <span class="lbl">views</span></div>
                            <div class="detail-stat">${Utils.formatNumber(sub.favorites_count)} <span class="lbl">faves</span></div>
                            <div class="detail-stat">${Utils.formatNumber(sub.comments_count)} <span class="lbl">comments</span></div>
                            <div class="detail-stat">${Utils.formatNumber(sub.downloads || 0)} <span class="lbl">downloads</span></div>
                        </div>
                        <div style="margin-top:8px;display:flex;gap:8px;align-items:center;flex-wrap:wrap">
                            <button class="btn ${isPinned ? 'btn-danger' : 'btn-secondary'} btn-pin" data-platform="da" data-id="${id}" style="padding:4px 10px;font-size:12px">${isPinned ? 'Unpin' : 'Pin'}</button>
                            ${currentTags.map(t => Components.tagBadge(t)).join('')}
                            <button class="btn btn-secondary btn-add-tag" data-platform="da" data-id="${id}" style="padding:4px 10px;font-size:12px">+ Tag</button>
                        </div>
                        <div style="margin-top:8px">${Components.keywords(sub.keywords)}</div>
                    </div>
                </div>

                ${Components.growthRateCards(data.growth_rates)}

                ${Components.dateRangeBar(this._dateRange)}

                <div class="chart-container">
                    <h3>Stats Over Time</h3>
                    <div class="chart-wrap"><canvas id="chart-detail"></canvas></div>
                </div>
            `;

            this._setContent(html);

            if (data.snapshots && data.snapshots.length > 0) {
                Charts.submissionLine('chart-detail', data.snapshots);
            }

            this._bindDateRange(async () => {
                const range = Utils.getDateRange(this._dateRange);
                const snaps = await API.getDASnapshots(id, range);
                Charts.submissionLine('chart-detail', snaps.snapshots);
            });

            this._bindDetailPinTag('da', id, allTags.tags || [], () => this.renderDADetail(id));
            this._startAutoRefresh(() => this.renderDADetail(id));
        } catch (err) {
            this._setContent(`<div class="empty-state"><h3>Error loading DA submission</h3><p>${Utils.escapeHtml(err.message)}</p></div>`);
        }
    },

    // ── DA Compare ─────────────────────────────────────────────
    // DA comparison page with Downloads as an additional metric option.

    async renderDACompare() {
        this._loading();
        try {
            const data = await API.getDASubmissions({ sort_by: 'views', order: 'desc', account_id: this._acctId('da') });
            const subs = data.submissions;

            const chips = subs.map(s => `
                <label class="compare-chip ${this._daCompareIds.has(s.submission_id) ? 'selected' : ''}" data-id="${s.submission_id}">
                    <input type="checkbox" ${this._daCompareIds.has(s.submission_id) ? 'checked' : ''}>
                    ${Utils.escapeHtml(Utils.truncate(s.title, 25))}
                </label>
            `).join('');

            const html = `
                ${this._refreshIndicatorHtml()}
                <div class="page-header">
                    <h2>Compare DA Submissions</h2>
                    <div>
                        <select class="filter-select" id="compare-metric">
                            <option value="views" ${this._daCompareMetric === 'views' ? 'selected' : ''}>Views</option>
                            <option value="favorites_count" ${this._daCompareMetric === 'favorites_count' ? 'selected' : ''}>Favourites</option>
                            <option value="comments_count" ${this._daCompareMetric === 'comments_count' ? 'selected' : ''}>Comments</option>
                            <option value="downloads" ${this._daCompareMetric === 'downloads' ? 'selected' : ''}>Downloads</option>
                        </select>
                    </div>
                </div>
                <p style="font-size:13px;color:var(--text-muted);margin-bottom:12px">Select 2-5 DA submissions to compare their trends over time.</p>
                <div class="compare-select">${chips}</div>

                ${Components.dateRangeBar(this._dateRange)}

                <div class="chart-container" id="compare-chart-container" style="${this._daCompareIds.size < 2 ? 'display:none' : ''}">
                    <h3>Comparison</h3>
                    <div class="chart-wrap"><canvas id="chart-compare"></canvas></div>
                </div>
                ${this._daCompareIds.size < 2 ? '<div class="empty-state"><p>Select at least 2 submissions above to see their trends compared.</p></div>' : ''}
            `;

            this._setContent(html);

            document.querySelectorAll('.compare-chip').forEach(chip => {
                chip.addEventListener('click', (e) => {
                    e.preventDefault();
                    const id = parseInt(chip.dataset.id);
                    if (this._daCompareIds.has(id)) {
                        this._daCompareIds.delete(id);
                    } else if (this._daCompareIds.size < 5) {
                        this._daCompareIds.add(id);
                    }
                    this.renderDACompare();
                });
            });

            const metricSelect = document.getElementById('compare-metric');
            if (metricSelect) {
                metricSelect.addEventListener('change', () => {
                    this._daCompareMetric = metricSelect.value;
                    this._loadDAComparisonChart();
                });
            }

            this._bindDateRange(() => this._loadDAComparisonChart());

            if (this._daCompareIds.size >= 2) {
                await this._loadDAComparisonChart();
            }

            this._startAutoRefresh(() => this.renderDACompare());
        } catch (err) {
            this._setContent(`<div class="empty-state"><h3>Error</h3><p>${Utils.escapeHtml(err.message)}</p></div>`);
        }
    },

    async _loadDAComparisonChart() {
        try {
            if (this._daCompareIds.size < 2) return;
            const range = Utils.getDateRange(this._dateRange);
            const data = await API.getDAComparison([...this._daCompareIds], range);
            const container = document.getElementById('compare-chart-container');
            if (container) container.style.display = '';
            Charts.comparisonLine('chart-compare', data.series, data.titles, this._daCompareMetric);
        } catch (e) {
            console.error('Failed to load DA comparison chart:', e);
        }
    },

    // ── WP Dashboard ─────────────────────────────────────────
    // Wattpad dashboard showing Reads, Votes, Comments, Lists stats.
    // Uses Wattpad-specific metric names throughout.

    async renderWPDashboard() {
        this._loading();
        try {
            const [summary, agg, pins, goals] = await Promise.all([
                API.getWPSummary({ account_id: this._acctId('wp') }),
                API.getWPAggregate({ ...Utils.getDateRange(this._dateRange), account_id: this._acctId('wp') }),
                API.getPins().catch(() => ({ pins: [] })),
                API.getGoals().catch(() => ({ goals: [] })),
            ]);
            const wpPins = (pins.pins || []).filter(p => p.platform === 'wp');
            const wpGoals = (goals.goals || []).filter(g => g.platform === 'wp' || g.platform === 'all');

            // Empty-state short-circuit: see SF dashboard for the pattern.
            const wpHealth = window.PlatformHealth && window.PlatformHealth.get('wp');
            const isUnconfigured = wpHealth && wpHealth.configured === false;
            if (isUnconfigured || (summary.total_submissions || 0) === 0) {
                this._setContent(`
                    ${this._refreshIndicatorHtml()}
                    <div class="page-header"><h2>Wattpad Dashboard</h2></div>
                    ${Components.platformEmptyState('wp', isUnconfigured ? {} : { reason: 'Wattpad is configured but no stories have been polled yet. The first poll may still be running.' })}
                `);
                return;
            }

            const html = `
                ${this._refreshIndicatorHtml()}
                <div class="page-header">
                    <h2>Wattpad Dashboard</h2>
                    <div style="display:flex;gap:8px">
                        <button class="btn btn-primary" data-poll="wp">Poll Now</button>
                        <button class="btn btn-secondary" data-resync="wp">Full Resync</button>
                        <button class="btn btn-secondary" data-export="wp">Export CSV</button>
                    </div>
                </div>

                ${wpPins.length ? Components.pinnedSubmissions(wpPins, 'wp') : ''}
                ${wpGoals.length ? `<div class="goals-section"><h3>Goals</h3>${Components.goalProgressCards(wpGoals)}</div>` : ''}

                <div class="stats-grid">
                    ${Components.statCard('Total Submissions', summary.total_submissions, null, '#/wp/submissions')}
                    ${Components.statCard('Total Reads', summary.total_reads || summary.total_views || 0)}
                    ${Components.statCard('Total Votes', summary.total_votes || summary.total_favorites || 0)}
                    ${Components.statCard('Total Comments', summary.total_comments)}
                    ${Components.statCard('Total Lists', summary.total_lists || summary.total_num_lists || 0)}
                </div>

                ${summary.growth_rates ? (() => {
                    /* Map Wattpad growth rate field names for the growthRateCards component */
                    const rates = {};
                    for (const period of ['24h', '7d', '30d']) {
                        const r = summary.growth_rates[period];
                        if (r) {
                            rates[period] = {
                                views_per_day: r.reads_per_day != null ? r.reads_per_day : r.views_per_day,
                                faves_per_day: r.votes_per_day != null ? r.votes_per_day : r.faves_per_day,
                                comments_per_day: r.comments_per_day,
                            };
                        }
                    }
                    return Components.growthRateCards(rates, { views: 'reads/day', faves: 'votes/day', comments: 'comments/day' });
                })() : ''}

                ${Components.dateRangeBar(this._dateRange)}

                <div class="chart-container">
                    <h3>Reads Over Time (Aggregate)</h3>
                    <div class="chart-wrap"><canvas id="chart-agg-reads"></canvas></div>
                </div>

                <div class="chart-row">
                    <div class="chart-container">
                        <h3>Top Read</h3>
                        ${Components.wpTopList(summary.top_read || summary.top_viewed, 'reads', 'title', 'submission_id')}
                    </div>
                    <div class="chart-container">
                        <h3>Top Voted</h3>
                        ${Components.wpTopList(summary.top_voted || summary.top_faved, 'votes', 'title', 'submission_id')}
                    </div>
                </div>

                <div class="chart-row">
                    <div class="chart-container">
                        <h3>Most Listed</h3>
                        ${Components.wpTopList(summary.top_listed, 'num_lists', 'title', 'submission_id')}
                    </div>
                    <div class="chart-container">
                        <h3>Fastest Growing (24h)</h3>
                        ${Components.wpTopList(summary.fastest_growing, 'reads_gained', 'title', 'submission_id')}
                    </div>
                </div>
            `;

            this._setContent(html);

            if (agg.snapshots && agg.snapshots.length > 0) {
                Charts.aggregateLine('chart-agg-reads', agg.snapshots, ['reads']);
            }

            this._loadFollowerWidget('wp', this._acctId('wp'));
            this._bindDateRange(() => this.renderWPDashboard());
            this._bindPinAndGoalActions(() => this.renderWPDashboard());
            this._startAutoRefresh(() => this.renderWPDashboard());
        } catch (err) {
            this._setContent(`<div class="empty-state"><h3>Error loading WP dashboard</h3><p>${Utils.escapeHtml(err.message)}</p></div>`);
        }
    },

    // ── WP Submissions ─────────────────────────────────────────
    // Wattpad submissions table with Reads, Votes, Comments, Lists columns.

    async renderWPSubmissions() {
        this._loading();
        try {
            const data = await API.getWPSubmissions({
                sort_by: this._wpSortState.field,
                order: this._wpSortState.order,
                account_id: this._acctId('wp'),
            });

            const _vm = localStorage.getItem('pp-view-mode') || 'grid';
            // 2.16.14 (BUG-021): closure so the search filter can re-render
            const wpGridRenderer = (subs) => Components.submissionCardGrid(subs, {
                idKey: 'submission_id', titleKey: 'title', thumbKey: 'cover_url', proxyThumb: false,
                detailRoute: '/wp/submission', dateKey: 'posted_at',
                stats: [
                    { key: 'reads', deltaKey: 'reads_delta', label: 'reads' },
                    { key: 'votes', deltaKey: 'votes_delta', label: 'votes' },
                    { key: 'comments_count', deltaKey: 'comments_delta', label: 'comments' },
                ],
            });
            const gridHtml = wpGridRenderer(data.submissions);
            const html = `
                ${this._refreshIndicatorHtml()}
                <div class="page-header"><h2>WP Submissions</h2></div>
                <div class="toolbar">
                    <input type="text" class="search-input" id="search-input" placeholder="Search titles...">
                    <div class="view-toggle">
                        <button class="view-toggle-btn ${_vm === 'grid' ? 'active' : ''}" data-view="grid" title="Grid view">&#9638;</button>
                        <button class="view-toggle-btn ${_vm === 'list' ? 'active' : ''}" data-view="list" title="List view">&#9776;</button>
                    </div>
                </div>
                <div id="grid-container" style="${_vm !== 'grid' ? 'display:none' : ''}">${gridHtml}</div>
                <div id="table-container" class="table-scroll" style="${_vm !== 'list' ? 'display:none' : ''}">
                    ${Components.wpSubmissionsTable(data.submissions)}
                </div>
            `;

            this._setContent(html);
            this._bindViewToggle();
            this._bindWPTableSort();
            this._bindWPSearch(data.submissions, wpGridRenderer);
            this._startAutoRefresh(() => this.renderWPSubmissions());
        } catch (err) {
            this._setContent(`<div class="empty-state"><h3>Error loading WP submissions</h3><p>${Utils.escapeHtml(err.message)}</p></div>`);
        }
    },

    // ── WP Submission Detail ───────────────────────────────────
    // Individual Wattpad submission detail with 4 metrics: reads, votes, comments, num_lists.

    async renderWPDetail(id) {
        this._loading();
        try {
            const [data, pins, allTags] = await Promise.all([
                API.getWPSubmission(id),
                API.getPins().catch(() => ({ pins: [] })),
                API.getTags().catch(() => ({ tags: [] })),
            ]);
            const sub = data.submission;
            const isPinned = (pins.pins || []).some(p => p.platform === 'wp' && p.submission_id === id);
            const currentTags = sub.tags || [];

            const html = `
                ${this._refreshIndicatorHtml()}
                <a href="#/wp/submissions" class="back-link">&larr; Back to WP Submissions</a>
                <div class="detail-header">
                    ${sub.cover_url ? `<img src="${Utils.escapeHtml(sub.cover_url)}" class="detail-thumb">` : ''}
                    <div class="detail-info">
                        <h2>${Utils.escapeHtml(sub.title)}</h2>
                        <div class="detail-meta">by ${Utils.escapeHtml(sub.username)} &middot; ${Utils.formatDate(sub.posted_at)}</div>
                        <div class="detail-meta"><a href="${Utils.escapeHtml(Utils.safeUrl(sub.link) || '#')}" target="_blank">View on Wattpad</a></div>
                        <div class="detail-stats">
                            <div class="detail-stat">${Utils.formatNumber(sub.reads || sub.views || 0)} <span class="lbl">reads</span></div>
                            <div class="detail-stat">${Utils.formatNumber(sub.votes || sub.favorites_count || 0)} <span class="lbl">votes</span></div>
                            <div class="detail-stat">${Utils.formatNumber(sub.comments_count || 0)} <span class="lbl">comments</span></div>
                            <div class="detail-stat">${Utils.formatNumber(sub.num_lists || 0)} <span class="lbl">lists</span></div>
                        </div>
                        <div style="margin-top:8px;display:flex;gap:8px;align-items:center;flex-wrap:wrap">
                            <button class="btn ${isPinned ? 'btn-danger' : 'btn-secondary'} btn-pin" data-platform="wp" data-id="${id}" style="padding:4px 10px;font-size:12px">${isPinned ? 'Unpin' : 'Pin'}</button>
                            ${currentTags.map(t => Components.tagBadge(t)).join('')}
                            <button class="btn btn-secondary btn-add-tag" data-platform="wp" data-id="${id}" style="padding:4px 10px;font-size:12px">+ Tag</button>
                        </div>
                        <div style="margin-top:8px">${Components.keywords(sub.keywords)}</div>
                    </div>
                </div>

                ${Components.growthRateCards(data.growth_rates, { views: 'reads/day', faves: 'votes/day', comments: 'comments/day' })}

                ${Components.dateRangeBar(this._dateRange)}

                <div class="chart-container">
                    <h3>Stats Over Time</h3>
                    <div class="chart-wrap"><canvas id="chart-detail"></canvas></div>
                </div>
            `;

            this._setContent(html);

            if (data.snapshots && data.snapshots.length > 0) {
                Charts.submissionLine('chart-detail', data.snapshots, ['reads', 'votes', 'comments_count', 'num_lists']);
            }

            this._bindDateRange(async () => {
                const range = Utils.getDateRange(this._dateRange);
                const snaps = await API.getWPSnapshots(id, range);
                Charts.submissionLine('chart-detail', snaps.snapshots, ['reads', 'votes', 'comments_count', 'num_lists']);
            });

            this._bindDetailPinTag('wp', id, allTags.tags || [], () => this.renderWPDetail(id));
            this._startAutoRefresh(() => this.renderWPDetail(id));
        } catch (err) {
            this._setContent(`<div class="empty-state"><h3>Error loading WP submission</h3><p>${Utils.escapeHtml(err.message)}</p></div>`);
        }
    },

    // ── WP Compare ─────────────────────────────────────────────
    // Wattpad comparison page with reads, votes, comments_count, num_lists metrics.

    async renderWPCompare() {
        this._loading();
        try {
            const data = await API.getWPSubmissions({ sort_by: 'reads', order: 'desc', account_id: this._acctId('wp') });
            const subs = data.submissions;

            const chips = subs.map(s => `
                <label class="compare-chip ${this._wpCompareIds.has(s.submission_id) ? 'selected' : ''}" data-id="${s.submission_id}">
                    <input type="checkbox" ${this._wpCompareIds.has(s.submission_id) ? 'checked' : ''}>
                    ${Utils.escapeHtml(Utils.truncate(s.title, 25))}
                </label>
            `).join('');

            const html = `
                ${this._refreshIndicatorHtml()}
                <div class="page-header">
                    <h2>Compare WP Submissions</h2>
                    <div>
                        <select class="filter-select" id="compare-metric">
                            <option value="reads" ${this._wpCompareMetric === 'reads' ? 'selected' : ''}>Reads</option>
                            <option value="votes" ${this._wpCompareMetric === 'votes' ? 'selected' : ''}>Votes</option>
                            <option value="comments_count" ${this._wpCompareMetric === 'comments_count' ? 'selected' : ''}>Comments</option>
                            <option value="num_lists" ${this._wpCompareMetric === 'num_lists' ? 'selected' : ''}>Lists</option>
                        </select>
                    </div>
                </div>
                <p style="font-size:13px;color:var(--text-muted);margin-bottom:12px">Select 2-5 WP submissions to compare their trends over time.</p>
                <div class="compare-select">${chips}</div>

                ${Components.dateRangeBar(this._dateRange)}

                <div class="chart-container" id="compare-chart-container" style="${this._wpCompareIds.size < 2 ? 'display:none' : ''}">
                    <h3>Comparison</h3>
                    <div class="chart-wrap"><canvas id="chart-compare"></canvas></div>
                </div>
                ${this._wpCompareIds.size < 2 ? '<div class="empty-state"><p>Select at least 2 submissions above to see their trends compared.</p></div>' : ''}
            `;

            this._setContent(html);

            document.querySelectorAll('.compare-chip').forEach(chip => {
                chip.addEventListener('click', (e) => {
                    e.preventDefault();
                    const id = parseInt(chip.dataset.id);
                    if (this._wpCompareIds.has(id)) {
                        this._wpCompareIds.delete(id);
                    } else if (this._wpCompareIds.size < 5) {
                        this._wpCompareIds.add(id);
                    }
                    this.renderWPCompare();
                });
            });

            const metricSelect = document.getElementById('compare-metric');
            if (metricSelect) {
                metricSelect.addEventListener('change', () => {
                    this._wpCompareMetric = metricSelect.value;
                    this._loadWPComparisonChart();
                });
            }

            this._bindDateRange(() => this._loadWPComparisonChart());

            if (this._wpCompareIds.size >= 2) {
                await this._loadWPComparisonChart();
            }

            this._startAutoRefresh(() => this.renderWPCompare());
        } catch (err) {
            this._setContent(`<div class="empty-state"><h3>Error</h3><p>${Utils.escapeHtml(err.message)}</p></div>`);
        }
    },

    async _loadWPComparisonChart() {
        try {
            if (this._wpCompareIds.size < 2) return;
            const range = Utils.getDateRange(this._dateRange);
            const data = await API.getWPComparison([...this._wpCompareIds], range);
            const container = document.getElementById('compare-chart-container');
            if (container) container.style.display = '';
            Charts.comparisonLine('chart-compare', data.series, data.titles, this._wpCompareMetric);
        } catch (e) {
            console.error('Failed to load WP comparison chart:', e);
        }
    },

    // ── IK Dashboard ─────────────────────────────────────────
    // Itaku dashboard with Likes, Comments, Reshares stat cards (NO views).

    async renderIKDashboard() {
        this._loading();
        try {
            const [summary, agg, pins, goals] = await Promise.all([
                API.getIKSummary({ account_id: this._acctId('ik') }),
                API.getIKAggregate({ ...Utils.getDateRange(this._dateRange), account_id: this._acctId('ik') }),
                API.getPins().catch(() => ({ pins: [] })),
                API.getGoals().catch(() => ({ goals: [] })),
            ]);
            const ikPins = (pins.pins || []).filter(p => p.platform === 'ik');
            const ikGoals = (goals.goals || []).filter(g => g.platform === 'ik' || g.platform === 'all');

            // Empty-state short-circuit: see SF dashboard for the pattern.
            const ikHealth = window.PlatformHealth && window.PlatformHealth.get('ik');
            const isUnconfigured = ikHealth && ikHealth.configured === false;
            if (isUnconfigured || (summary.total_submissions || 0) === 0) {
                this._setContent(`
                    ${this._refreshIndicatorHtml()}
                    <div class="page-header"><h2>Itaku Dashboard</h2></div>
                    ${Components.platformEmptyState('ik', isUnconfigured ? {} : { reason: 'Itaku is configured but no content has been polled yet. The first poll may still be running.' })}
                `);
                return;
            }

            const html = `
                ${this._refreshIndicatorHtml()}
                <div class="page-header">
                    <h2>Itaku Dashboard</h2>
                    <div style="display:flex;gap:8px">
                        <button class="btn btn-primary" data-poll="ik">Poll Now</button>
                        <button class="btn btn-secondary" data-resync="ik">Full Resync</button>
                        <button class="btn btn-secondary" data-export="ik">Export CSV</button>
                    </div>
                </div>

                ${ikPins.length ? Components.pinnedSubmissions(ikPins, 'ik') : ''}
                ${ikGoals.length ? `<div class="goals-section"><h3>Goals</h3>${Components.goalProgressCards(ikGoals)}</div>` : ''}

                <div class="stats-grid">
                    ${Components.statCard('Total Submissions', summary.total_submissions, null, '#/ik/submissions')}
                    ${Components.statCard('Total Likes', summary.total_likes || 0)}
                    ${Components.statCard('Total Comments', summary.total_comments)}
                    ${Components.statCard('Total Reshares', summary.total_reshares || 0)}
                </div>

                ${summary.growth_rates ? (() => {
                    const rates = {};
                    for (const period of ['24h', '7d', '30d']) {
                        const r = summary.growth_rates[period];
                        if (r) {
                            rates[period] = {
                                views_per_day: r.likes_per_day != null ? r.likes_per_day : 0,
                                faves_per_day: r.reshares_per_day != null ? r.reshares_per_day : 0,
                                comments_per_day: r.comments_per_day,
                            };
                        }
                    }
                    return Components.growthRateCards(rates, { views: 'likes/day', faves: 'reshares/day', comments: 'comments/day' });
                })() : ''}

                ${Components.dateRangeBar(this._dateRange)}

                <div class="chart-container">
                    <h3>Likes Over Time (Aggregate)</h3>
                    <div class="chart-wrap"><canvas id="chart-agg-likes"></canvas></div>
                </div>

                <div class="chart-row">
                    <div class="chart-container">
                        <h3>Top Liked</h3>
                        ${Components.ikTopList(summary.top_liked || summary.top_faved, 'likes', 'title', 'submission_id')}
                    </div>
                    <div class="chart-container">
                        <h3>Top Reshared</h3>
                        ${Components.ikTopList(summary.top_reshared, 'reshares', 'title', 'submission_id')}
                    </div>
                </div>

                <div class="chart-row">
                    <div class="chart-container">
                        <h3>Fastest Growing (24h)</h3>
                        ${Components.ikTopList(summary.fastest_growing, 'likes_gained', 'title', 'submission_id')}
                    </div>
                </div>
            `;

            this._setContent(html);

            if (agg.snapshots && agg.snapshots.length > 0) {
                Charts.aggregateLine('chart-agg-likes', agg.snapshots, ['likes']);
            }

            this._loadFollowerWidget('ik', this._acctId('ik'));
            this._bindDateRange(() => this.renderIKDashboard());
            this._bindPinAndGoalActions(() => this.renderIKDashboard());
            this._startAutoRefresh(() => this.renderIKDashboard());
        } catch (err) {
            this._setContent(`<div class="empty-state"><h3>Error loading IK dashboard</h3><p>${Utils.escapeHtml(err.message)}</p></div>`);
        }
    },

    // ── IK Submissions ─────────────────────────────────────────
    // Itaku submissions table with Type, Likes, Comments, Reshares columns (no views).

    async renderIKSubmissions() {
        this._loading();
        try {
            const data = await API.getIKSubmissions({
                sort_by: this._ikSortState.field,
                order: this._ikSortState.order,
                account_id: this._acctId('ik'),
            });

            const _vm = localStorage.getItem('pp-view-mode') || 'grid';
            // 2.16.14 (BUG-021): closure so the search filter can re-render
            const ikGridRenderer = (subs) => Components.submissionCardGrid(subs, {
                idKey: 'submission_id', titleKey: 'title', thumbKey: 'thumbnail_url', proxyThumb: false,
                detailRoute: '/ik/submission', dateKey: 'posted_at',
                stats: [
                    { key: 'likes', deltaKey: 'likes_delta', label: 'likes' },
                    { key: 'comments_count', deltaKey: 'comments_delta', label: 'comments' },
                    { key: 'reshares', deltaKey: 'reshares_delta', label: 'reshares' },
                ],
            });
            const gridHtml = ikGridRenderer(data.submissions);
            const html = `
                ${this._refreshIndicatorHtml()}
                <div class="page-header"><h2>IK Submissions</h2></div>
                <div class="toolbar">
                    <input type="text" class="search-input" id="search-input" placeholder="Search titles...">
                    <div class="view-toggle">
                        <button class="view-toggle-btn ${_vm === 'grid' ? 'active' : ''}" data-view="grid" title="Grid view">&#9638;</button>
                        <button class="view-toggle-btn ${_vm === 'list' ? 'active' : ''}" data-view="list" title="List view">&#9776;</button>
                    </div>
                </div>
                <div id="grid-container" style="${_vm !== 'grid' ? 'display:none' : ''}">${gridHtml}</div>
                <div id="table-container" class="table-scroll" style="${_vm !== 'list' ? 'display:none' : ''}">
                    ${Components.ikSubmissionsTable(data.submissions)}
                </div>
            `;

            this._setContent(html);
            this._bindViewToggle();
            this._bindIKTableSort();
            this._bindIKSearch(data.submissions, ikGridRenderer);
            this._startAutoRefresh(() => this.renderIKSubmissions());
        } catch (err) {
            this._setContent(`<div class="empty-state"><h3>Error loading IK submissions</h3><p>${Utils.escapeHtml(err.message)}</p></div>`);
        }
    },

    // ── IK Submission Detail ───────────────────────────────────
    // Individual Itaku submission detail with 3 metrics: likes, comments, reshares (no views).

    async renderIKDetail(id) {
        this._loading();
        try {
            const [data, pins, allTags] = await Promise.all([
                API.getIKSubmission(id),
                API.getPins().catch(() => ({ pins: [] })),
                API.getTags().catch(() => ({ tags: [] })),
            ]);
            const sub = data.submission;
            const isPinned = (pins.pins || []).some(p => p.platform === 'ik' && p.submission_id === id);
            const currentTags = sub.tags || [];

            const html = `
                ${this._refreshIndicatorHtml()}
                <a href="#/ik/submissions" class="back-link">&larr; Back to IK Submissions</a>
                <div class="detail-header">
                    ${sub.thumbnail_url ? `<img src="${Utils.escapeHtml(sub.thumbnail_url)}" class="detail-thumb">` : ''}
                    <div class="detail-info">
                        <h2>${Utils.escapeHtml(sub.title)}</h2>
                        <div class="detail-meta">by ${Utils.escapeHtml(sub.username)} &middot; ${Utils.formatDate(sub.posted_at)} &middot; ${Utils.escapeHtml(Components.BSKY_TYPE_LABELS[sub.content_type] || sub.content_type || 'Post')}</div>
                        <div class="detail-meta"><a href="${Utils.escapeHtml(Utils.safeUrl(sub.link) || '#')}" target="_blank">View on Itaku</a></div>
                        <div class="detail-stats">
                            <div class="detail-stat">${Utils.formatNumber(sub.likes || 0)} <span class="lbl">likes</span></div>
                            <div class="detail-stat">${Utils.formatNumber(sub.comments_count || 0)} <span class="lbl">comments</span></div>
                            <div class="detail-stat">${Utils.formatNumber(sub.reshares || 0)} <span class="lbl">reshares</span></div>
                        </div>
                        <div style="margin-top:8px;display:flex;gap:8px;align-items:center;flex-wrap:wrap">
                            <button class="btn ${isPinned ? 'btn-danger' : 'btn-secondary'} btn-pin" data-platform="ik" data-id="${id}" style="padding:4px 10px;font-size:12px">${isPinned ? 'Unpin' : 'Pin'}</button>
                            ${currentTags.map(t => Components.tagBadge(t)).join('')}
                            <button class="btn btn-secondary btn-add-tag" data-platform="ik" data-id="${id}" style="padding:4px 10px;font-size:12px">+ Tag</button>
                        </div>
                        <div style="margin-top:8px">${Components.keywords(sub.keywords)}</div>
                    </div>
                </div>

                ${Components.growthRateCards(data.growth_rates, { views: 'likes/day', faves: 'reshares/day', comments: 'comments/day' })}

                ${Components.dateRangeBar(this._dateRange)}

                <div class="chart-container">
                    <h3>Stats Over Time</h3>
                    <div class="chart-wrap"><canvas id="chart-detail"></canvas></div>
                </div>
            `;

            this._setContent(html);

            if (data.snapshots && data.snapshots.length > 0) {
                Charts.submissionLine('chart-detail', data.snapshots, ['likes', 'comments_count', 'reshares']);
            }

            this._bindDateRange(async () => {
                const range = Utils.getDateRange(this._dateRange);
                const snaps = await API.getIKSnapshots(id, range);
                Charts.submissionLine('chart-detail', snaps.snapshots, ['likes', 'comments_count', 'reshares']);
            });

            this._bindDetailPinTag('ik', id, allTags.tags || [], () => this.renderIKDetail(id));
            this._startAutoRefresh(() => this.renderIKDetail(id));
        } catch (err) {
            this._setContent(`<div class="empty-state"><h3>Error loading IK submission</h3><p>${Utils.escapeHtml(err.message)}</p></div>`);
        }
    },

    // ── IK Compare ─────────────────────────────────────────────
    // Itaku comparison page with likes, comments_count, reshares metrics (no views).

    async renderIKCompare() {
        this._loading();
        try {
            const data = await API.getIKSubmissions({ sort_by: 'likes', order: 'desc', account_id: this._acctId('ik') });
            const subs = data.submissions;

            const chips = subs.map(s => `
                <label class="compare-chip ${this._ikCompareIds.has(s.submission_id) ? 'selected' : ''}" data-id="${s.submission_id}">
                    <input type="checkbox" ${this._ikCompareIds.has(s.submission_id) ? 'checked' : ''}>
                    ${Utils.escapeHtml(Utils.truncate(s.title, 25))}
                </label>
            `).join('');

            const html = `
                ${this._refreshIndicatorHtml()}
                <div class="page-header">
                    <h2>Compare IK Submissions</h2>
                    <div>
                        <select class="filter-select" id="compare-metric">
                            <option value="likes" ${this._ikCompareMetric === 'likes' ? 'selected' : ''}>Likes</option>
                            <option value="comments_count" ${this._ikCompareMetric === 'comments_count' ? 'selected' : ''}>Comments</option>
                            <option value="reshares" ${this._ikCompareMetric === 'reshares' ? 'selected' : ''}>Reshares</option>
                        </select>
                    </div>
                </div>
                <p style="font-size:13px;color:var(--text-muted);margin-bottom:12px">Select 2-5 IK submissions to compare their trends over time.</p>
                <div class="compare-select">${chips}</div>

                ${Components.dateRangeBar(this._dateRange)}

                <div class="chart-container" id="compare-chart-container" style="${this._ikCompareIds.size < 2 ? 'display:none' : ''}">
                    <h3>Comparison</h3>
                    <div class="chart-wrap"><canvas id="chart-compare"></canvas></div>
                </div>
                ${this._ikCompareIds.size < 2 ? '<div class="empty-state"><p>Select at least 2 submissions above to see their trends compared.</p></div>' : ''}
            `;

            this._setContent(html);

            document.querySelectorAll('.compare-chip').forEach(chip => {
                chip.addEventListener('click', (e) => {
                    e.preventDefault();
                    const id = parseInt(chip.dataset.id);
                    if (this._ikCompareIds.has(id)) {
                        this._ikCompareIds.delete(id);
                    } else if (this._ikCompareIds.size < 5) {
                        this._ikCompareIds.add(id);
                    }
                    this.renderIKCompare();
                });
            });

            const metricSelect = document.getElementById('compare-metric');
            if (metricSelect) {
                metricSelect.addEventListener('change', () => {
                    this._ikCompareMetric = metricSelect.value;
                    this._loadIKComparisonChart();
                });
            }

            this._bindDateRange(() => this._loadIKComparisonChart());

            if (this._ikCompareIds.size >= 2) {
                await this._loadIKComparisonChart();
            }

            this._startAutoRefresh(() => this.renderIKCompare());
        } catch (err) {
            this._setContent(`<div class="empty-state"><h3>Error</h3><p>${Utils.escapeHtml(err.message)}</p></div>`);
        }
    },

    async _loadIKComparisonChart() {
        try {
            if (this._ikCompareIds.size < 2) return;
            const range = Utils.getDateRange(this._dateRange);
            const data = await API.getIKComparison([...this._ikCompareIds], range);
            const container = document.getElementById('compare-chart-container');
            if (container) container.style.display = '';
            Charts.comparisonLine('chart-compare', data.series, data.titles, this._ikCompareMetric);
        } catch (e) {
            console.error('Failed to load IK comparison chart:', e);
        }
    },

    // ── BSKY Dashboard ─────────────────────────────────────────
    // Bluesky dashboard with Likes, Reposts, Replies, Quotes stat cards (NO views).

    async renderBSKYDashboard() {
        this._loading();
        try {
            const [summary, agg, pins, goals] = await Promise.all([
                API.getBSKYSummary({ account_id: this._acctId('bsky') }),
                API.getBSKYAggregate({ ...Utils.getDateRange(this._dateRange), account_id: this._acctId('bsky') }),
                API.getPins().catch(() => ({ pins: [] })),
                API.getGoals().catch(() => ({ goals: [] })),
            ]);
            const bskyPins = (pins.pins || []).filter(p => p.platform === 'bsky');
            const bskyGoals = (goals.goals || []).filter(g => g.platform === 'bsky' || g.platform === 'all');

            // Empty-state short-circuit: see SF dashboard for the pattern.
            const bskyHealth = window.PlatformHealth && window.PlatformHealth.get('bsky');
            const isUnconfigured = bskyHealth && bskyHealth.configured === false;
            if (isUnconfigured || (summary.total_submissions || 0) === 0) {
                this._setContent(`
                    ${this._refreshIndicatorHtml()}
                    <div class="page-header"><h2>Bluesky Dashboard</h2></div>
                    ${Components.platformEmptyState('bsky', isUnconfigured ? {} : { reason: 'Bluesky is configured but no posts have been polled yet. The first poll may still be running.' })}
                `);
                return;
            }

            const html = `
                ${this._refreshIndicatorHtml()}
                <div class="page-header">
                    <h2>Bluesky Dashboard</h2>
                    <div style="display:flex;gap:8px">
                        <button class="btn btn-primary" data-poll="bsky">Poll Now</button>
                        <button class="btn btn-secondary" data-resync="bsky">Full Resync</button>
                        <button class="btn btn-secondary" data-export="bsky">Export CSV</button>
                    </div>
                </div>

                ${bskyPins.length ? Components.pinnedSubmissions(bskyPins, 'bsky') : ''}
                ${bskyGoals.length ? `<div class="goals-section"><h3>Goals</h3>${Components.goalProgressCards(bskyGoals)}</div>` : ''}

                <div class="stats-grid">
                    ${Components.statCard('Total Posts', summary.total_submissions, null, '#/bsky/submissions')}
                    ${Components.statCard('Total Likes', summary.total_likes || 0)}
                    ${Components.statCard('Total Reposts', summary.total_reposts || 0)}
                    ${Components.statCard('Total Replies', summary.total_comments || summary.total_replies || 0)}
                    ${Components.statCard('Total Quotes', summary.total_quotes || 0)}
                </div>

                ${summary.growth_rates ? (() => {
                    const rates = {};
                    for (const period of ['24h', '7d', '30d']) {
                        const r = summary.growth_rates[period];
                        if (r) {
                            rates[period] = {
                                views_per_day: r.likes_per_day != null ? r.likes_per_day : 0,
                                faves_per_day: r.reposts_per_day != null ? r.reposts_per_day : 0,
                                comments_per_day: r.comments_per_day || r.replies_per_day || 0,
                            };
                        }
                    }
                    return Components.growthRateCards(rates, { views: 'likes/day', faves: 'reposts/day', comments: 'replies/day' });
                })() : ''}

                ${Components.dateRangeBar(this._dateRange)}

                <div class="chart-container">
                    <h3>Likes Over Time (Aggregate)</h3>
                    <div class="chart-wrap"><canvas id="chart-agg-likes"></canvas></div>
                </div>

                <div class="chart-row">
                    <div class="chart-container">
                        <h3>Top Liked</h3>
                        ${Components.bskyTopList(summary.top_liked || summary.top_faved, 'likes', 'title', 'submission_id')}
                    </div>
                    <div class="chart-container">
                        <h3>Top Reposted</h3>
                        ${Components.bskyTopList(summary.top_reposted, 'reposts', 'title', 'submission_id')}
                    </div>
                </div>

                <div class="chart-row">
                    <div class="chart-container">
                        <h3>Fastest Growing (24h)</h3>
                        ${Components.bskyTopList(summary.fastest_growing, 'likes_gained', 'title', 'submission_id')}
                    </div>
                </div>
            `;

            this._setContent(html);

            if (agg.snapshots && agg.snapshots.length > 0) {
                Charts.aggregateLine('chart-agg-likes', agg.snapshots, ['likes']);
            }

            this._loadFollowerWidget('bsky', this._acctId('bsky'));
            this._bindDateRange(() => this.renderBSKYDashboard());
            this._bindPinAndGoalActions(() => this.renderBSKYDashboard());
            this._startAutoRefresh(() => this.renderBSKYDashboard());
        } catch (err) {
            this._setContent(`<div class="empty-state"><h3>Error loading BSKY dashboard</h3><p>${Utils.escapeHtml(err.message)}</p></div>`);
        }
    },

    // ── BSKY Submissions ─────────────────────────────────────────

    async renderBSKYSubmissions() {
        this._loading();
        try {
            const data = await API.getBSKYSubmissions({
                sort_by: this._bskySortState.field,
                order: this._bskySortState.order,
                account_id: this._acctId('bsky'),
            });

            const _vm = localStorage.getItem('pp-view-mode') || 'grid';
            // BSKY uses rkey (last segment of submission_id URI) for routing.
            // 2.16.14 (BUG-021): closure so the search filter can re-render.
            const bskyGridRenderer = (subs) => Components.submissionCardGrid(
                subs.map(s => ({ ...s, _rkey: String(s.submission_id).split('/').pop() })),
                {
                    idKey: '_rkey', titleKey: 'title', thumbKey: 'thumbnail_url', proxyThumb: false,
                    typeKey: 'content_type', typeLabels: Components.BSKY_TYPE_LABELS,
                    detailRoute: '/bsky/submission', dateKey: 'posted_at',
                    stats: [
                        { key: 'likes', deltaKey: 'likes_delta', label: 'likes' },
                        { key: 'reposts', deltaKey: 'reposts_delta', label: 'reposts' },
                        { key: 'replies', deltaKey: 'replies_delta', label: 'replies' },
                    ],
                }
            );
            const gridHtml = bskyGridRenderer(data.submissions);
            const html = `
                ${this._refreshIndicatorHtml()}
                <div class="page-header"><h2>Bluesky Posts</h2></div>
                <div class="toolbar">
                    <input type="text" class="search-input" id="search-input" placeholder="Search posts...">
                    <div class="view-toggle">
                        <button class="view-toggle-btn ${_vm === 'grid' ? 'active' : ''}" data-view="grid" title="Grid view">&#9638;</button>
                        <button class="view-toggle-btn ${_vm === 'list' ? 'active' : ''}" data-view="list" title="List view">&#9776;</button>
                    </div>
                </div>
                <div id="grid-container" style="${_vm !== 'grid' ? 'display:none' : ''}">${gridHtml}</div>
                <div id="table-container" class="table-scroll" style="${_vm !== 'list' ? 'display:none' : ''}">
                    ${Components.bskySubmissionsTable(data.submissions)}
                </div>
            `;

            this._setContent(html);
            this._bindViewToggle();
            this._bindBSKYTableSort();
            this._bindBSKYSearch(data.submissions, bskyGridRenderer);
            this._startAutoRefresh(() => this.renderBSKYSubmissions());
        } catch (err) {
            this._setContent(`<div class="empty-state"><h3>Error loading BSKY submissions</h3><p>${Utils.escapeHtml(err.message)}</p></div>`);
        }
    },

    // ── BSKY Submission Detail ───────────────────────────────────

    async renderBSKYDetail(rkey) {
        this._loading();
        try {
            const [data, pins, allTags] = await Promise.all([
                API.getBSKYSubmission(rkey),
                API.getPins().catch(() => ({ pins: [] })),
                API.getTags().catch(() => ({ tags: [] })),
            ]);
            const sub = data.submission;
            const fullId = sub.submission_id;
            const isPinned = (pins.pins || []).some(p => p.platform === 'bsky' && String(p.submission_id) === String(fullId));
            const currentTags = sub.tags || [];

            const html = `
                ${this._refreshIndicatorHtml()}
                <a href="#/bsky/submissions" class="back-link">&larr; Back to Bluesky Posts</a>
                <div class="detail-header">
                    <div class="detail-info">
                        <h2>${Utils.escapeHtml(sub.title)}</h2>
                        <div class="detail-meta">by ${Utils.escapeHtml(sub.username)} &middot; ${Utils.formatDate(sub.posted_at)}</div>
                        <div class="detail-meta"><a href="${Utils.escapeHtml(Utils.safeUrl(sub.link) || '#')}" target="_blank">View on Bluesky</a></div>
                        <div class="detail-stats">
                            <div class="detail-stat">${Utils.formatNumber(sub.likes || 0)} <span class="lbl">likes</span></div>
                            <div class="detail-stat">${Utils.formatNumber(sub.reposts || 0)} <span class="lbl">reposts</span></div>
                            <div class="detail-stat">${Utils.formatNumber(sub.replies || 0)} <span class="lbl">replies</span></div>
                            <div class="detail-stat">${Utils.formatNumber(sub.quotes || 0)} <span class="lbl">quotes</span></div>
                        </div>
                        <div style="margin-top:8px;display:flex;gap:8px;align-items:center;flex-wrap:wrap">
                            <button class="btn ${isPinned ? 'btn-danger' : 'btn-secondary'} btn-pin" data-platform="bsky" data-id="${Utils.escapeHtml(fullId)}" style="padding:4px 10px;font-size:12px">${isPinned ? 'Unpin' : 'Pin'}</button>
                            ${currentTags.map(t => Components.tagBadge(t)).join('')}
                            <button class="btn btn-secondary btn-add-tag" data-platform="bsky" data-id="${Utils.escapeHtml(fullId)}" style="padding:4px 10px;font-size:12px">+ Tag</button>
                        </div>
                        <div style="margin-top:8px">${Components.keywords(sub.keywords)}</div>
                    </div>
                </div>

                ${Components.growthRateCards(data.growth_rates, { views: 'likes/day', faves: 'reposts/day', comments: 'replies/day' })}

                ${Components.dateRangeBar(this._dateRange)}

                <div class="chart-container">
                    <h3>Stats Over Time</h3>
                    <div class="chart-wrap"><canvas id="chart-detail"></canvas></div>
                </div>
            `;

            this._setContent(html);

            if (data.snapshots && data.snapshots.length > 0) {
                Charts.submissionLine('chart-detail', data.snapshots, ['likes', 'reposts', 'replies', 'quotes']);
            }

            this._bindDateRange(async () => {
                const range = Utils.getDateRange(this._dateRange);
                const snaps = await API.getBSKYSnapshots(rkey, range);
                Charts.submissionLine('chart-detail', snaps.snapshots, ['likes', 'reposts', 'replies', 'quotes']);
            });

            this._bindDetailPinTag('bsky', fullId, allTags.tags || [], () => this.renderBSKYDetail(rkey));
            this._startAutoRefresh(() => this.renderBSKYDetail(rkey));
        } catch (err) {
            this._setContent(`<div class="empty-state"><h3>Error loading BSKY post</h3><p>${Utils.escapeHtml(err.message)}</p></div>`);
        }
    },

    // ── BSKY Compare ─────────────────────────────────────────────

    async renderBSKYCompare() {
        this._loading();
        try {
            const data = await API.getBSKYSubmissions({ sort_by: 'likes', order: 'desc', account_id: this._acctId('bsky') });
            const subs = data.submissions;

            const chips = subs.map(s => {
                const rkey = String(s.submission_id).split('/').pop();
                return `
                <label class="compare-chip ${this._bskyCompareIds.has(rkey) ? 'selected' : ''}" data-id="${rkey}" data-full-id="${Utils.escapeHtml(s.submission_id)}">
                    <input type="checkbox" ${this._bskyCompareIds.has(rkey) ? 'checked' : ''}>
                    ${Utils.escapeHtml(Utils.truncate(s.title, 25))}
                </label>
            `;
            }).join('');

            const html = `
                ${this._refreshIndicatorHtml()}
                <div class="page-header">
                    <h2>Compare Bluesky Posts</h2>
                    <div>
                        <select class="filter-select" id="compare-metric">
                            <option value="likes" ${this._bskyCompareMetric === 'likes' ? 'selected' : ''}>Likes</option>
                            <option value="reposts" ${this._bskyCompareMetric === 'reposts' ? 'selected' : ''}>Reposts</option>
                            <option value="replies" ${this._bskyCompareMetric === 'replies' ? 'selected' : ''}>Replies</option>
                            <option value="quotes" ${this._bskyCompareMetric === 'quotes' ? 'selected' : ''}>Quotes</option>
                        </select>
                    </div>
                </div>
                <p style="font-size:13px;color:var(--text-muted);margin-bottom:12px">Select 2-5 Bluesky posts to compare their trends over time.</p>
                <div class="compare-select">${chips}</div>

                ${Components.dateRangeBar(this._dateRange)}

                <div class="chart-container" id="compare-chart-container" style="${this._bskyCompareIds.size < 2 ? 'display:none' : ''}">
                    <h3>Comparison</h3>
                    <div class="chart-wrap"><canvas id="chart-compare"></canvas></div>
                </div>
                ${this._bskyCompareIds.size < 2 ? '<div class="empty-state"><p>Select at least 2 posts above to see their trends compared.</p></div>' : ''}
            `;

            this._setContent(html);

            document.querySelectorAll('.compare-chip').forEach(chip => {
                chip.addEventListener('click', (e) => {
                    e.preventDefault();
                    const id = chip.dataset.id;
                    if (this._bskyCompareIds.has(id)) {
                        this._bskyCompareIds.delete(id);
                    } else if (this._bskyCompareIds.size < 5) {
                        this._bskyCompareIds.add(id);
                    }
                    this.renderBSKYCompare();
                });
            });

            const metricSelect = document.getElementById('compare-metric');
            if (metricSelect) {
                metricSelect.addEventListener('change', () => {
                    this._bskyCompareMetric = metricSelect.value;
                    this._loadBSKYComparisonChart();
                });
            }

            this._bindDateRange(() => this._loadBSKYComparisonChart());

            if (this._bskyCompareIds.size >= 2) {
                await this._loadBSKYComparisonChart();
            }

            this._startAutoRefresh(() => this.renderBSKYCompare());
        } catch (err) {
            this._setContent(`<div class="empty-state"><h3>Error</h3><p>${Utils.escapeHtml(err.message)}</p></div>`);
        }
    },

    async _loadBSKYComparisonChart() {
        try {
            if (this._bskyCompareIds.size < 2) return;
            const range = Utils.getDateRange(this._dateRange);
            const data = await API.getBSKYComparison([...this._bskyCompareIds], range);
            const container = document.getElementById('compare-chart-container');
            if (container) container.style.display = '';
            Charts.comparisonLine('chart-compare', data.series, data.titles, this._bskyCompareMetric);
        } catch (e) {
            console.error('Failed to load BSKY comparison chart:', e);
        }
    },

    // ── MAST Dashboard ─────────────────────────────────────────
    // Mastodon dashboard with Likes (favourites), Reposts (boosts), Replies.
    // Mastodon has no quote metric, so there's no Quotes card (mirrors bsky otherwise).

    async renderMASTDashboard() {
        this._loading();
        try {
            const [summary, agg, pins, goals] = await Promise.all([
                API.getMASTSummary({ account_id: this._acctId('mast') }),
                API.getMASTAggregate({ ...Utils.getDateRange(this._dateRange), account_id: this._acctId('mast') }),
                API.getPins().catch(() => ({ pins: [] })),
                API.getGoals().catch(() => ({ goals: [] })),
            ]);
            const mastPins = (pins.pins || []).filter(p => p.platform === 'mast');
            const mastGoals = (goals.goals || []).filter(g => g.platform === 'mast' || g.platform === 'all');

            // Empty-state short-circuit: see SF dashboard for the pattern.
            const mastHealth = window.PlatformHealth && window.PlatformHealth.get('mast');
            const isUnconfigured = mastHealth && mastHealth.configured === false;
            if (isUnconfigured || (summary.total_submissions || 0) === 0) {
                this._setContent(`
                    ${this._refreshIndicatorHtml()}
                    <div class="page-header"><h2>Mastodon Dashboard</h2></div>
                    ${Components.platformEmptyState('mast', isUnconfigured ? {} : { reason: 'Mastodon is configured but no posts have been polled yet. The first poll may still be running.' })}
                `);
                return;
            }

            const html = `
                ${this._refreshIndicatorHtml()}
                <div class="page-header">
                    <h2>Mastodon Dashboard</h2>
                    <div style="display:flex;gap:8px">
                        <button class="btn btn-primary" data-poll="mast">Poll Now</button>
                        <button class="btn btn-secondary" data-resync="mast">Full Resync</button>
                        <button class="btn btn-secondary" data-export="mast">Export CSV</button>
                    </div>
                </div>

                ${mastPins.length ? Components.pinnedSubmissions(mastPins, 'mast') : ''}
                ${mastGoals.length ? `<div class="goals-section"><h3>Goals</h3>${Components.goalProgressCards(mastGoals)}</div>` : ''}

                <div class="stats-grid">
                    ${Components.statCard('Total Posts', summary.total_submissions, null, '#/mast/submissions')}
                    ${Components.statCard('Total Likes', summary.total_likes || 0)}
                    ${Components.statCard('Total Reposts', summary.total_reposts || 0)}
                    ${Components.statCard('Total Replies', summary.total_comments || summary.total_replies || 0)}
                </div>

                ${summary.growth_rates ? (() => {
                    const rates = {};
                    for (const period of ['24h', '7d', '30d']) {
                        const r = summary.growth_rates[period];
                        if (r) {
                            rates[period] = {
                                views_per_day: r.likes_per_day != null ? r.likes_per_day : 0,
                                faves_per_day: r.reposts_per_day != null ? r.reposts_per_day : 0,
                                comments_per_day: r.comments_per_day || r.replies_per_day || 0,
                            };
                        }
                    }
                    return Components.growthRateCards(rates, { views: 'likes/day', faves: 'reposts/day', comments: 'replies/day' });
                })() : ''}

                ${Components.dateRangeBar(this._dateRange)}

                <div class="chart-container">
                    <h3>Likes Over Time (Aggregate)</h3>
                    <div class="chart-wrap"><canvas id="chart-agg-likes"></canvas></div>
                </div>

                <div class="chart-row">
                    <div class="chart-container">
                        <h3>Top Liked</h3>
                        ${Components.mastTopList(summary.top_liked || summary.top_faved, 'likes', 'title', 'submission_id')}
                    </div>
                    <div class="chart-container">
                        <h3>Top Reposted</h3>
                        ${Components.mastTopList(summary.top_reposted, 'reposts', 'title', 'submission_id')}
                    </div>
                </div>

                <div class="chart-row">
                    <div class="chart-container">
                        <h3>Fastest Growing (24h)</h3>
                        ${Components.mastTopList(summary.fastest_growing, 'likes_gained', 'title', 'submission_id')}
                    </div>
                </div>
            `;

            this._setContent(html);

            if (agg.snapshots && agg.snapshots.length > 0) {
                Charts.aggregateLine('chart-agg-likes', agg.snapshots, ['likes']);
            }

            this._loadFollowerWidget('mast', this._acctId('mast'));
            this._bindDateRange(() => this.renderMASTDashboard());
            this._bindPinAndGoalActions(() => this.renderMASTDashboard());
            this._startAutoRefresh(() => this.renderMASTDashboard());
        } catch (err) {
            this._setContent(`<div class="empty-state"><h3>Error loading MAST dashboard</h3><p>${Utils.escapeHtml(err.message)}</p></div>`);
        }
    },

    // ── MAST Submissions ─────────────────────────────────────────

    async renderMASTSubmissions() {
        this._loading();
        try {
            const data = await API.getMASTSubmissions({
                sort_by: this._mastSortState.field,
                order: this._mastSortState.order,
                account_id: this._acctId('mast'),
            });

            const _vm = localStorage.getItem('pp-view-mode') || 'grid';
            // MAST uses rkey (last segment of submission_id URI) for routing.
            const mastGridRenderer = (subs) => Components.submissionCardGrid(
                subs.map(s => ({ ...s, _rkey: String(s.submission_id).split('/').pop() })),
                {
                    idKey: '_rkey', titleKey: 'title', thumbKey: 'thumbnail_url', proxyThumb: false,
                    typeKey: 'content_type', typeLabels: Components.MAST_TYPE_LABELS,
                    detailRoute: '/mast/submission', dateKey: 'posted_at',
                    stats: [
                        { key: 'likes', deltaKey: 'likes_delta', label: 'likes' },
                        { key: 'reposts', deltaKey: 'reposts_delta', label: 'reposts' },
                        { key: 'replies', deltaKey: 'replies_delta', label: 'replies' },
                    ],
                }
            );
            const gridHtml = mastGridRenderer(data.submissions);
            const html = `
                ${this._refreshIndicatorHtml()}
                <div class="page-header"><h2>Mastodon Posts</h2></div>
                <div class="toolbar">
                    <input type="text" class="search-input" id="search-input" placeholder="Search posts...">
                    <div class="view-toggle">
                        <button class="view-toggle-btn ${_vm === 'grid' ? 'active' : ''}" data-view="grid" title="Grid view">&#9638;</button>
                        <button class="view-toggle-btn ${_vm === 'list' ? 'active' : ''}" data-view="list" title="List view">&#9776;</button>
                    </div>
                </div>
                <div id="grid-container" style="${_vm !== 'grid' ? 'display:none' : ''}">${gridHtml}</div>
                <div id="table-container" class="table-scroll" style="${_vm !== 'list' ? 'display:none' : ''}">
                    ${Components.mastSubmissionsTable(data.submissions)}
                </div>
            `;

            this._setContent(html);
            this._bindViewToggle();
            this._bindMASTTableSort();
            this._bindMASTSearch(data.submissions, mastGridRenderer);
            this._startAutoRefresh(() => this.renderMASTSubmissions());
        } catch (err) {
            this._setContent(`<div class="empty-state"><h3>Error loading MAST submissions</h3><p>${Utils.escapeHtml(err.message)}</p></div>`);
        }
    },

    // ── MAST Submission Detail ───────────────────────────────────

    async renderMASTDetail(rkey) {
        this._loading();
        try {
            const [data, pins, allTags] = await Promise.all([
                API.getMASTSubmission(rkey),
                API.getPins().catch(() => ({ pins: [] })),
                API.getTags().catch(() => ({ tags: [] })),
            ]);
            const sub = data.submission;
            const fullId = sub.submission_id;
            const isPinned = (pins.pins || []).some(p => p.platform === 'mast' && String(p.submission_id) === String(fullId));
            const currentTags = sub.tags || [];

            const html = `
                ${this._refreshIndicatorHtml()}
                <a href="#/mast/submissions" class="back-link">&larr; Back to Mastodon Posts</a>
                <div class="detail-header">
                    <div class="detail-info">
                        <h2>${Utils.escapeHtml(sub.title)}</h2>
                        <div class="detail-meta">by ${Utils.escapeHtml(sub.username)} &middot; ${Utils.formatDate(sub.posted_at)} &middot; ${Utils.escapeHtml(Components.MAST_TYPE_LABELS[sub.content_type] || sub.content_type || 'Post')}</div>
                        <div class="detail-meta"><a href="${Utils.escapeHtml(Utils.safeUrl(sub.link) || '#')}" target="_blank">View on Mastodon</a></div>
                        <div class="detail-stats">
                            <div class="detail-stat">${Utils.formatNumber(sub.likes || 0)} <span class="lbl">likes</span></div>
                            <div class="detail-stat">${Utils.formatNumber(sub.reposts || 0)} <span class="lbl">reposts</span></div>
                            <div class="detail-stat">${Utils.formatNumber(sub.replies || 0)} <span class="lbl">replies</span></div>
                        </div>
                        <div style="margin-top:8px;display:flex;gap:8px;align-items:center;flex-wrap:wrap">
                            <button class="btn ${isPinned ? 'btn-danger' : 'btn-secondary'} btn-pin" data-platform="mast" data-id="${Utils.escapeHtml(fullId)}" style="padding:4px 10px;font-size:12px">${isPinned ? 'Unpin' : 'Pin'}</button>
                            ${currentTags.map(t => Components.tagBadge(t)).join('')}
                            <button class="btn btn-secondary btn-add-tag" data-platform="mast" data-id="${Utils.escapeHtml(fullId)}" style="padding:4px 10px;font-size:12px">+ Tag</button>
                        </div>
                        <div style="margin-top:8px">${Components.keywords(sub.keywords)}</div>
                    </div>
                </div>

                ${Components.growthRateCards(data.growth_rates, { views: 'likes/day', faves: 'reposts/day', comments: 'replies/day' })}

                ${Components.dateRangeBar(this._dateRange)}

                <div class="chart-container">
                    <h3>Stats Over Time</h3>
                    <div class="chart-wrap"><canvas id="chart-detail"></canvas></div>
                </div>
            `;

            this._setContent(html);

            if (data.snapshots && data.snapshots.length > 0) {
                Charts.submissionLine('chart-detail', data.snapshots, ['likes', 'reposts', 'replies']);
            }

            this._bindDateRange(async () => {
                const range = Utils.getDateRange(this._dateRange);
                const snaps = await API.getMASTSnapshots(rkey, range);
                Charts.submissionLine('chart-detail', snaps.snapshots, ['likes', 'reposts', 'replies']);
            });

            this._bindDetailPinTag('mast', fullId, allTags.tags || [], () => this.renderMASTDetail(rkey));
            this._startAutoRefresh(() => this.renderMASTDetail(rkey));
        } catch (err) {
            this._setContent(`<div class="empty-state"><h3>Error loading MAST post</h3><p>${Utils.escapeHtml(err.message)}</p></div>`);
        }
    },

    // ── MAST Compare ─────────────────────────────────────────────

    async renderMASTCompare() {
        this._loading();
        try {
            const data = await API.getMASTSubmissions({ sort_by: 'likes', order: 'desc', account_id: this._acctId('mast') });
            const subs = data.submissions;

            const chips = subs.map(s => {
                const rkey = String(s.submission_id).split('/').pop();
                return `
                <label class="compare-chip ${this._mastCompareIds.has(rkey) ? 'selected' : ''}" data-id="${rkey}" data-full-id="${Utils.escapeHtml(s.submission_id)}">
                    <input type="checkbox" ${this._mastCompareIds.has(rkey) ? 'checked' : ''}>
                    ${Utils.escapeHtml(Utils.truncate(s.title, 25))}
                </label>
            `;
            }).join('');

            const html = `
                ${this._refreshIndicatorHtml()}
                <div class="page-header">
                    <h2>Compare Mastodon Posts</h2>
                    <div>
                        <select class="filter-select" id="compare-metric">
                            <option value="likes" ${this._mastCompareMetric === 'likes' ? 'selected' : ''}>Likes</option>
                            <option value="reposts" ${this._mastCompareMetric === 'reposts' ? 'selected' : ''}>Reposts</option>
                            <option value="replies" ${this._mastCompareMetric === 'replies' ? 'selected' : ''}>Replies</option>
                        </select>
                    </div>
                </div>
                <p style="font-size:13px;color:var(--text-muted);margin-bottom:12px">Select 2-5 Mastodon posts to compare their trends over time.</p>
                <div class="compare-select">${chips}</div>

                ${Components.dateRangeBar(this._dateRange)}

                <div class="chart-container" id="compare-chart-container" style="${this._mastCompareIds.size < 2 ? 'display:none' : ''}">
                    <h3>Comparison</h3>
                    <div class="chart-wrap"><canvas id="chart-compare"></canvas></div>
                </div>
                ${this._mastCompareIds.size < 2 ? '<div class="empty-state"><p>Select at least 2 posts above to see their trends compared.</p></div>' : ''}
            `;

            this._setContent(html);

            document.querySelectorAll('.compare-chip').forEach(chip => {
                chip.addEventListener('click', (e) => {
                    e.preventDefault();
                    const id = chip.dataset.id;
                    if (this._mastCompareIds.has(id)) {
                        this._mastCompareIds.delete(id);
                    } else if (this._mastCompareIds.size < 5) {
                        this._mastCompareIds.add(id);
                    }
                    this.renderMASTCompare();
                });
            });

            const metricSelect = document.getElementById('compare-metric');
            if (metricSelect) {
                metricSelect.addEventListener('change', () => {
                    this._mastCompareMetric = metricSelect.value;
                    this._loadMASTComparisonChart();
                });
            }

            this._bindDateRange(() => this._loadMASTComparisonChart());

            if (this._mastCompareIds.size >= 2) {
                await this._loadMASTComparisonChart();
            }

            this._startAutoRefresh(() => this.renderMASTCompare());
        } catch (err) {
            this._setContent(`<div class="empty-state"><h3>Error</h3><p>${Utils.escapeHtml(err.message)}</p></div>`);
        }
    },

    async _loadMASTComparisonChart() {
        try {
            if (this._mastCompareIds.size < 2) return;
            const range = Utils.getDateRange(this._dateRange);
            const data = await API.getMASTComparison([...this._mastCompareIds], range);
            const container = document.getElementById('compare-chart-container');
            if (container) container.style.display = '';
            Charts.comparisonLine('chart-compare', data.series, data.titles, this._mastCompareMetric);
        } catch (e) {
            console.error('Failed to load MAST comparison chart:', e);
        }
    },

    // ── TUM Dashboard ─────────────────────────────────────────
    // Tumblr dashboard with a single engagement metric: Notes.

    async renderTUMDashboard() {
        this._loading();
        try {
            const [summary, agg, pins, goals] = await Promise.all([
                API.getTUMSummary({ account_id: this._acctId('tum') }),
                API.getTUMAggregate({ ...Utils.getDateRange(this._dateRange), account_id: this._acctId('tum') }),
                API.getPins().catch(() => ({ pins: [] })),
                API.getGoals().catch(() => ({ goals: [] })),
            ]);
            const tumPins = (pins.pins || []).filter(p => p.platform === 'tum');
            const tumGoals = (goals.goals || []).filter(g => g.platform === 'tum' || g.platform === 'all');

            const tumHealth = window.PlatformHealth && window.PlatformHealth.get('tum');
            const isUnconfigured = tumHealth && tumHealth.configured === false;
            if (isUnconfigured || (summary.total_submissions || 0) === 0) {
                this._setContent(`
                    ${this._refreshIndicatorHtml()}
                    <div class="page-header"><h2>Tumblr Dashboard</h2></div>
                    ${Components.platformEmptyState('tum', isUnconfigured ? {} : { reason: 'Tumblr is configured but no posts have been polled yet. The first poll may still be running.' })}
                `);
                return;
            }

            const html = `
                ${this._refreshIndicatorHtml()}
                <div class="page-header">
                    <h2>Tumblr Dashboard</h2>
                    <div style="display:flex;gap:8px">
                        <button class="btn btn-primary" data-poll="tum">Poll Now</button>
                        <button class="btn btn-secondary" data-resync="tum">Full Resync</button>
                        <button class="btn btn-secondary" data-export="tum">Export CSV</button>
                    </div>
                </div>

                ${tumPins.length ? Components.pinnedSubmissions(tumPins, 'tum') : ''}
                ${tumGoals.length ? `<div class="goals-section"><h3>Goals</h3>${Components.goalProgressCards(tumGoals)}</div>` : ''}

                <div class="stats-grid">
                    ${Components.statCard('Total Posts', summary.total_submissions, null, '#/tum/submissions')}
                    ${Components.statCard('Total Notes', summary.total_notes || 0)}
                </div>

                ${Components.dateRangeBar(this._dateRange)}

                <div class="chart-container">
                    <h3>Notes Over Time (Aggregate)</h3>
                    <div class="chart-wrap"><canvas id="chart-agg-notes"></canvas></div>
                </div>

                <div class="chart-row">
                    <div class="chart-container">
                        <h3>Most Notes</h3>
                        ${Components.tumTopList(summary.top_noted, 'notes', 'title', 'submission_id')}
                    </div>
                    <div class="chart-container">
                        <h3>Fastest Growing (24h)</h3>
                        ${Components.tumTopList(summary.fastest_growing, 'notes_gained', 'title', 'submission_id')}
                    </div>
                </div>
            `;

            this._setContent(html);

            if (agg.snapshots && agg.snapshots.length > 0) {
                Charts.aggregateLine('chart-agg-notes', agg.snapshots, ['notes']);
            }

            this._bindDateRange(() => this.renderTUMDashboard());
            this._bindPinAndGoalActions(() => this.renderTUMDashboard());
            this._startAutoRefresh(() => this.renderTUMDashboard());
        } catch (err) {
            this._setContent(`<div class="empty-state"><h3>Error loading TUM dashboard</h3><p>${Utils.escapeHtml(err.message)}</p></div>`);
        }
    },

    // ── TUM Submissions ─────────────────────────────────────────

    async renderTUMSubmissions() {
        this._loading();
        try {
            const data = await API.getTUMSubmissions({
                sort_by: this._tumSortState.field,
                order: this._tumSortState.order,
                account_id: this._acctId('tum'),
            });

            const _vm = localStorage.getItem('pp-view-mode') || 'grid';
            const tumGridRenderer = (subs) => Components.submissionCardGrid(
                subs,
                {
                    idKey: 'submission_id', titleKey: 'title', thumbKey: 'thumbnail_url', proxyThumb: false,
                    typeKey: 'content_type', typeLabels: Components.TUM_TYPE_LABELS,
                    detailRoute: '/tum/submission', dateKey: 'posted_at',
                    stats: [
                        { key: 'notes', deltaKey: 'notes_delta', label: 'notes' },
                    ],
                }
            );
            const gridHtml = tumGridRenderer(data.submissions);
            const html = `
                ${this._refreshIndicatorHtml()}
                <div class="page-header"><h2>Tumblr Posts</h2></div>
                <div class="toolbar">
                    <input type="text" class="search-input" id="search-input" placeholder="Search posts...">
                    <div class="view-toggle">
                        <button class="view-toggle-btn ${_vm === 'grid' ? 'active' : ''}" data-view="grid" title="Grid view">&#9638;</button>
                        <button class="view-toggle-btn ${_vm === 'list' ? 'active' : ''}" data-view="list" title="List view">&#9776;</button>
                    </div>
                </div>
                <div id="grid-container" style="${_vm !== 'grid' ? 'display:none' : ''}">${gridHtml}</div>
                <div id="table-container" class="table-scroll" style="${_vm !== 'list' ? 'display:none' : ''}">
                    ${Components.tumSubmissionsTable(data.submissions)}
                </div>
            `;

            this._setContent(html);
            this._bindViewToggle();
            this._bindTUMTableSort();
            this._bindTUMSearch(data.submissions, tumGridRenderer);
            this._startAutoRefresh(() => this.renderTUMSubmissions());
        } catch (err) {
            this._setContent(`<div class="empty-state"><h3>Error loading TUM submissions</h3><p>${Utils.escapeHtml(err.message)}</p></div>`);
        }
    },

    // ── TUM Submission Detail ───────────────────────────────────

    async renderTUMDetail(postId) {
        this._loading();
        try {
            const [data, pins, allTags] = await Promise.all([
                API.getTUMSubmission(postId),
                API.getPins().catch(() => ({ pins: [] })),
                API.getTags().catch(() => ({ tags: [] })),
            ]);
            const sub = data.submission;
            const fullId = sub.submission_id;
            const isPinned = (pins.pins || []).some(p => p.platform === 'tum' && String(p.submission_id) === String(fullId));
            const currentTags = sub.tags || [];

            const html = `
                ${this._refreshIndicatorHtml()}
                <a href="#/tum/submissions" class="back-link">&larr; Back to Tumblr Posts</a>
                <div class="detail-header">
                    <div class="detail-info">
                        <h2>${Utils.escapeHtml(sub.title)}</h2>
                        <div class="detail-meta">by ${Utils.escapeHtml(sub.username)} &middot; ${Utils.formatDate(sub.posted_at)} &middot; ${Utils.escapeHtml(Components.TUM_TYPE_LABELS[sub.content_type] || sub.content_type || 'Text')}</div>
                        <div class="detail-meta"><a href="${Utils.escapeHtml(Utils.safeUrl(sub.link) || '#')}" target="_blank">View on Tumblr</a></div>
                        <div class="detail-stats">
                            <div class="detail-stat">${Utils.formatNumber(sub.notes || 0)} <span class="lbl">notes</span></div>
                        </div>
                        <div style="margin-top:8px;display:flex;gap:8px;align-items:center;flex-wrap:wrap">
                            <button class="btn ${isPinned ? 'btn-danger' : 'btn-secondary'} btn-pin" data-platform="tum" data-id="${Utils.escapeHtml(fullId)}" style="padding:4px 10px;font-size:12px">${isPinned ? 'Unpin' : 'Pin'}</button>
                            ${currentTags.map(t => Components.tagBadge(t)).join('')}
                            <button class="btn btn-secondary btn-add-tag" data-platform="tum" data-id="${Utils.escapeHtml(fullId)}" style="padding:4px 10px;font-size:12px">+ Tag</button>
                        </div>
                        <div style="margin-top:8px">${Components.keywords(sub.keywords)}</div>
                    </div>
                </div>

                ${Components.dateRangeBar(this._dateRange)}

                <div class="chart-container">
                    <h3>Notes Over Time</h3>
                    <div class="chart-wrap"><canvas id="chart-detail"></canvas></div>
                </div>
            `;

            this._setContent(html);

            if (data.snapshots && data.snapshots.length > 0) {
                Charts.submissionLine('chart-detail', data.snapshots, ['notes']);
            }

            this._bindDateRange(async () => {
                const range = Utils.getDateRange(this._dateRange);
                const snaps = await API.getTUMSnapshots(postId, range);
                Charts.submissionLine('chart-detail', snaps.snapshots, ['notes']);
            });

            this._bindDetailPinTag('tum', fullId, allTags.tags || [], () => this.renderTUMDetail(postId));
            this._startAutoRefresh(() => this.renderTUMDetail(postId));
        } catch (err) {
            this._setContent(`<div class="empty-state"><h3>Error loading TUM post</h3><p>${Utils.escapeHtml(err.message)}</p></div>`);
        }
    },

    // ── TUM Compare ─────────────────────────────────────────────

    async renderTUMCompare() {
        this._loading();
        try {
            const data = await API.getTUMSubmissions({ sort_by: 'notes', order: 'desc', account_id: this._acctId('tum') });
            const subs = data.submissions;

            const chips = subs.map(s => `
                <label class="compare-chip ${this._tumCompareIds.has(String(s.submission_id)) ? 'selected' : ''}" data-id="${Utils.escapeHtml(String(s.submission_id))}">
                    <input type="checkbox" ${this._tumCompareIds.has(String(s.submission_id)) ? 'checked' : ''}>
                    ${Utils.escapeHtml(Utils.truncate(s.title, 25))}
                </label>
            `).join('');

            const html = `
                ${this._refreshIndicatorHtml()}
                <div class="page-header"><h2>Compare Tumblr Posts</h2></div>
                <p style="font-size:13px;color:var(--text-muted);margin-bottom:12px">Select 2-5 Tumblr posts to compare their notes over time.</p>
                <div class="compare-select">${chips}</div>

                ${Components.dateRangeBar(this._dateRange)}

                <div class="chart-container" id="compare-chart-container" style="${this._tumCompareIds.size < 2 ? 'display:none' : ''}">
                    <h3>Comparison</h3>
                    <div class="chart-wrap"><canvas id="chart-compare"></canvas></div>
                </div>
                ${this._tumCompareIds.size < 2 ? '<div class="empty-state"><p>Select at least 2 posts above to see their notes compared.</p></div>' : ''}
            `;

            this._setContent(html);

            document.querySelectorAll('.compare-chip').forEach(chip => {
                chip.addEventListener('click', (e) => {
                    e.preventDefault();
                    const id = chip.dataset.id;
                    if (this._tumCompareIds.has(id)) {
                        this._tumCompareIds.delete(id);
                    } else if (this._tumCompareIds.size < 5) {
                        this._tumCompareIds.add(id);
                    }
                    this.renderTUMCompare();
                });
            });

            this._bindDateRange(() => this._loadTUMComparisonChart());

            if (this._tumCompareIds.size >= 2) {
                await this._loadTUMComparisonChart();
            }

            this._startAutoRefresh(() => this.renderTUMCompare());
        } catch (err) {
            this._setContent(`<div class="empty-state"><h3>Error</h3><p>${Utils.escapeHtml(err.message)}</p></div>`);
        }
    },

    async _loadTUMComparisonChart() {
        try {
            if (this._tumCompareIds.size < 2) return;
            const range = Utils.getDateRange(this._dateRange);
            const data = await API.getTUMComparison([...this._tumCompareIds], range);
            const container = document.getElementById('compare-chart-container');
            if (container) container.style.display = '';
            Charts.comparisonLine('chart-compare', data.series, data.titles, 'notes');
        } catch (e) {
            console.error('Failed to load TUM comparison chart:', e);
        }
    },

    // ── PIX Dashboard ─────────────────────────────────────────
    // Pixiv dashboard with gallery metrics: Views, Bookmarks, Comments.

    async renderPIXDashboard() {
        this._loading();
        try {
            const [summary, agg, pins, goals] = await Promise.all([
                API.getPIXSummary({ account_id: this._acctId('pix') }),
                API.getPIXAggregate({ ...Utils.getDateRange(this._dateRange), account_id: this._acctId('pix') }),
                API.getPins().catch(() => ({ pins: [] })),
                API.getGoals().catch(() => ({ goals: [] })),
            ]);
            const pixPins = (pins.pins || []).filter(p => p.platform === 'pix');
            const pixGoals = (goals.goals || []).filter(g => g.platform === 'pix' || g.platform === 'all');

            const pixHealth = window.PlatformHealth && window.PlatformHealth.get('pix');
            const isUnconfigured = pixHealth && pixHealth.configured === false;
            if (isUnconfigured || (summary.total_submissions || 0) === 0) {
                this._setContent(`
                    ${this._refreshIndicatorHtml()}
                    <div class="page-header"><h2>Pixiv Dashboard</h2></div>
                    ${Components.platformEmptyState('pix', isUnconfigured ? {} : { reason: 'Pixiv is configured but no works have been polled yet. The first poll may still be running.' })}
                `);
                return;
            }

            const html = `
                ${this._refreshIndicatorHtml()}
                <div class="page-header">
                    <h2>Pixiv Dashboard</h2>
                    <div style="display:flex;gap:8px">
                        <button class="btn btn-primary" data-poll="pix">Poll Now</button>
                        <button class="btn btn-secondary" data-resync="pix">Full Resync</button>
                        <button class="btn btn-secondary" data-export="pix">Export CSV</button>
                    </div>
                </div>

                ${pixPins.length ? Components.pinnedSubmissions(pixPins, 'pix') : ''}
                ${pixGoals.length ? `<div class="goals-section"><h3>Goals</h3>${Components.goalProgressCards(pixGoals)}</div>` : ''}

                <div class="stats-grid">
                    ${Components.statCard('Total Works', summary.total_submissions, null, '#/pix/submissions')}
                    ${Components.statCard('Total Views', summary.total_views || 0)}
                    ${Components.statCard('Total Bookmarks', summary.total_favorites || 0)}
                    ${Components.statCard('Total Comments', summary.total_comments || 0)}
                </div>

                ${summary.growth_rates ? Components.growthRateCards(summary.growth_rates, { views: 'views/day', faves: 'bookmarks/day', comments: 'comments/day' }) : ''}

                ${Components.dateRangeBar(this._dateRange)}

                <div class="chart-container">
                    <h3>Views Over Time (Aggregate)</h3>
                    <div class="chart-wrap"><canvas id="chart-agg-views"></canvas></div>
                </div>

                <div class="chart-row">
                    <div class="chart-container">
                        <h3>Top Viewed</h3>
                        ${Components.pixTopList(summary.top_viewed, 'views', 'title', 'submission_id')}
                    </div>
                    <div class="chart-container">
                        <h3>Top Bookmarked</h3>
                        ${Components.pixTopList(summary.top_faved, 'favorites_count', 'title', 'submission_id')}
                    </div>
                </div>

                <div class="chart-row">
                    <div class="chart-container">
                        <h3>Fastest Growing (24h)</h3>
                        ${Components.pixTopList(summary.fastest_growing, 'views_gained', 'title', 'submission_id')}
                    </div>
                </div>
            `;

            this._setContent(html);

            if (agg.snapshots && agg.snapshots.length > 0) {
                Charts.aggregateLine('chart-agg-views', agg.snapshots, ['views']);
            }

            this._loadFollowerWidget('pix', this._acctId('pix'));
            this._bindDateRange(() => this.renderPIXDashboard());
            this._bindPinAndGoalActions(() => this.renderPIXDashboard());
            this._startAutoRefresh(() => this.renderPIXDashboard());
        } catch (err) {
            this._setContent(`<div class="empty-state"><h3>Error loading PIX dashboard</h3><p>${Utils.escapeHtml(err.message)}</p></div>`);
        }
    },

    // ── PIX Submissions ─────────────────────────────────────────

    async renderPIXSubmissions() {
        this._loading();
        try {
            const data = await API.getPIXSubmissions({
                sort_by: this._pixSortState.field,
                order: this._pixSortState.order,
                account_id: this._acctId('pix'),
            });

            const _vm = localStorage.getItem('pp-view-mode') || 'grid';
            // Pixiv CDN thumbnails 403 without a pixiv Referer — route them
            // through the backend proxy and skip the generic proxy step.
            const pixGridRenderer = (subs) => Components.submissionCardGrid(
                subs.map(s => ({ ...s, _thumb: Utils.pixThumbUrl(s.thumbnail_url) })),
                {
                    idKey: 'submission_id', titleKey: 'title', thumbKey: '_thumb', proxyThumb: false,
                    typeKey: 'content_type', typeLabels: Components.PIX_TYPE_LABELS,
                    detailRoute: '/pix/submission', dateKey: 'posted_at',
                    stats: [
                        { key: 'views', deltaKey: 'views_delta', label: 'views' },
                        { key: 'favorites_count', deltaKey: 'favorites_delta', label: 'bookmarks' },
                        { key: 'comments_count', deltaKey: 'comments_delta', label: 'comments' },
                    ],
                }
            );
            const gridHtml = pixGridRenderer(data.submissions);
            const html = `
                ${this._refreshIndicatorHtml()}
                <div class="page-header"><h2>Pixiv Works</h2></div>
                <div class="toolbar">
                    <input type="text" class="search-input" id="search-input" placeholder="Search works...">
                    <div class="view-toggle">
                        <button class="view-toggle-btn ${_vm === 'grid' ? 'active' : ''}" data-view="grid" title="Grid view">&#9638;</button>
                        <button class="view-toggle-btn ${_vm === 'list' ? 'active' : ''}" data-view="list" title="List view">&#9776;</button>
                    </div>
                </div>
                <div id="grid-container" style="${_vm !== 'grid' ? 'display:none' : ''}">${gridHtml}</div>
                <div id="table-container" class="table-scroll" style="${_vm !== 'list' ? 'display:none' : ''}">
                    ${Components.pixSubmissionsTable(data.submissions)}
                </div>
            `;

            this._setContent(html);
            this._bindViewToggle();
            this._bindPIXTableSort();
            this._bindPIXSearch(data.submissions, pixGridRenderer);
            this._startAutoRefresh(() => this.renderPIXSubmissions());
        } catch (err) {
            this._setContent(`<div class="empty-state"><h3>Error loading PIX submissions</h3><p>${Utils.escapeHtml(err.message)}</p></div>`);
        }
    },

    // ── PIX Submission Detail ───────────────────────────────────

    async renderPIXDetail(workId) {
        this._loading();
        try {
            const [data, pins, allTags] = await Promise.all([
                API.getPIXSubmission(workId),
                API.getPins().catch(() => ({ pins: [] })),
                API.getTags().catch(() => ({ tags: [] })),
            ]);
            const sub = data.submission;
            const fullId = sub.submission_id;
            const isPinned = (pins.pins || []).some(p => p.platform === 'pix' && String(p.submission_id) === String(fullId));
            const currentTags = sub.tags || [];

            const html = `
                ${this._refreshIndicatorHtml()}
                <a href="#/pix/submissions" class="back-link">&larr; Back to Pixiv Works</a>
                <div class="detail-header">
                    ${sub.thumbnail_url ? `<img class="detail-thumb" src="${Utils.pixThumbUrl(sub.thumbnail_url)}" alt="" style="max-width:160px;border-radius:8px;margin-right:16px">` : ''}
                    <div class="detail-info">
                        <h2>${Utils.escapeHtml(sub.title)}</h2>
                        <div class="detail-meta">by ${Utils.escapeHtml(sub.username)} &middot; ${Utils.formatDate(sub.posted_at)} &middot; ${Utils.escapeHtml(Components.PIX_TYPE_LABELS[sub.content_type] || sub.content_type || 'Illust')}${sub.rating && sub.rating !== 'General' ? ' &middot; ' + Utils.escapeHtml(sub.rating) : ''}</div>
                        <div class="detail-meta"><a href="${Utils.escapeHtml(Utils.safeUrl(sub.link) || '#')}" target="_blank">View on Pixiv</a></div>
                        <div class="detail-stats">
                            <div class="detail-stat">${Utils.formatNumber(sub.views || 0)} <span class="lbl">views</span></div>
                            <div class="detail-stat">${Utils.formatNumber(sub.favorites_count || 0)} <span class="lbl">bookmarks</span></div>
                            <div class="detail-stat">${Utils.formatNumber(sub.comments_count || 0)} <span class="lbl">comments</span></div>
                        </div>
                        <div style="margin-top:8px;display:flex;gap:8px;align-items:center;flex-wrap:wrap">
                            <button class="btn ${isPinned ? 'btn-danger' : 'btn-secondary'} btn-pin" data-platform="pix" data-id="${Utils.escapeHtml(fullId)}" style="padding:4px 10px;font-size:12px">${isPinned ? 'Unpin' : 'Pin'}</button>
                            ${currentTags.map(t => Components.tagBadge(t)).join('')}
                            <button class="btn btn-secondary btn-add-tag" data-platform="pix" data-id="${Utils.escapeHtml(fullId)}" style="padding:4px 10px;font-size:12px">+ Tag</button>
                        </div>
                        <div style="margin-top:8px">${Components.keywords(sub.keywords)}</div>
                    </div>
                </div>

                ${Components.growthRateCards(data.growth_rates, { views: 'views/day', faves: 'bookmarks/day', comments: 'comments/day' })}

                ${Components.dateRangeBar(this._dateRange)}

                <div class="chart-container">
                    <h3>Stats Over Time</h3>
                    <div class="chart-wrap"><canvas id="chart-detail"></canvas></div>
                </div>
            `;

            this._setContent(html);

            if (data.snapshots && data.snapshots.length > 0) {
                Charts.submissionLine('chart-detail', data.snapshots, ['views', 'favorites_count', 'comments_count']);
            }

            this._bindDateRange(async () => {
                const range = Utils.getDateRange(this._dateRange);
                const snaps = await API.getPIXSnapshots(workId, range);
                Charts.submissionLine('chart-detail', snaps.snapshots, ['views', 'favorites_count', 'comments_count']);
            });

            this._bindDetailPinTag('pix', fullId, allTags.tags || [], () => this.renderPIXDetail(workId));
            this._startAutoRefresh(() => this.renderPIXDetail(workId));
        } catch (err) {
            this._setContent(`<div class="empty-state"><h3>Error loading PIX work</h3><p>${Utils.escapeHtml(err.message)}</p></div>`);
        }
    },

    // ── PIX Compare ─────────────────────────────────────────────

    async renderPIXCompare() {
        this._loading();
        try {
            const data = await API.getPIXSubmissions({ sort_by: 'views', order: 'desc', account_id: this._acctId('pix') });
            const subs = data.submissions;

            const chips = subs.map(s => `
                <label class="compare-chip ${this._pixCompareIds.has(String(s.submission_id)) ? 'selected' : ''}" data-id="${Utils.escapeHtml(String(s.submission_id))}">
                    <input type="checkbox" ${this._pixCompareIds.has(String(s.submission_id)) ? 'checked' : ''}>
                    ${Utils.escapeHtml(Utils.truncate(s.title, 25))}
                </label>
            `).join('');

            const html = `
                ${this._refreshIndicatorHtml()}
                <div class="page-header">
                    <h2>Compare Pixiv Works</h2>
                    <div>
                        <select class="filter-select" id="compare-metric">
                            <option value="views" ${this._pixCompareMetric === 'views' ? 'selected' : ''}>Views</option>
                            <option value="favorites_count" ${this._pixCompareMetric === 'favorites_count' ? 'selected' : ''}>Bookmarks</option>
                            <option value="comments_count" ${this._pixCompareMetric === 'comments_count' ? 'selected' : ''}>Comments</option>
                        </select>
                    </div>
                </div>
                <p style="font-size:13px;color:var(--text-muted);margin-bottom:12px">Select 2-5 Pixiv works to compare their trends over time.</p>
                <div class="compare-select">${chips}</div>

                ${Components.dateRangeBar(this._dateRange)}

                <div class="chart-container" id="compare-chart-container" style="${this._pixCompareIds.size < 2 ? 'display:none' : ''}">
                    <h3>Comparison</h3>
                    <div class="chart-wrap"><canvas id="chart-compare"></canvas></div>
                </div>
                ${this._pixCompareIds.size < 2 ? '<div class="empty-state"><p>Select at least 2 works above to see their trends compared.</p></div>' : ''}
            `;

            this._setContent(html);

            document.querySelectorAll('.compare-chip').forEach(chip => {
                chip.addEventListener('click', (e) => {
                    e.preventDefault();
                    const id = chip.dataset.id;
                    if (this._pixCompareIds.has(id)) {
                        this._pixCompareIds.delete(id);
                    } else if (this._pixCompareIds.size < 5) {
                        this._pixCompareIds.add(id);
                    }
                    this.renderPIXCompare();
                });
            });

            const metricSelect = document.getElementById('compare-metric');
            if (metricSelect) {
                metricSelect.addEventListener('change', () => {
                    this._pixCompareMetric = metricSelect.value;
                    this._loadPIXComparisonChart();
                });
            }

            this._bindDateRange(() => this._loadPIXComparisonChart());

            if (this._pixCompareIds.size >= 2) {
                await this._loadPIXComparisonChart();
            }

            this._startAutoRefresh(() => this.renderPIXCompare());
        } catch (err) {
            this._setContent(`<div class="empty-state"><h3>Error</h3><p>${Utils.escapeHtml(err.message)}</p></div>`);
        }
    },

    async _loadPIXComparisonChart() {
        try {
            if (this._pixCompareIds.size < 2) return;
            const range = Utils.getDateRange(this._dateRange);
            const data = await API.getPIXComparison([...this._pixCompareIds], range);
            const container = document.getElementById('compare-chart-container');
            if (container) container.style.display = '';
            Charts.comparisonLine('chart-compare', data.series, data.titles, this._pixCompareMetric);
        } catch (e) {
            console.error('Failed to load PIX comparison chart:', e);
        }
    },

    // ── E621 Dashboard ────────────────────────────────────────
    // e621 dashboard: Score (score.total, may be negative), Favorites,
    // Comments. e621 exposes no view count, so Score is the headline metric.

    // ── FurryNetwork Dashboard / Submissions / Detail / Compare ──

    async renderFNDashboard() {
        this._loading();
        try {
            const [summary, agg, pins, goals] = await Promise.all([
                API.getFNSummary({ account_id: this._acctId('fn') }),
                API.getFNAggregate({ ...Utils.getDateRange(this._dateRange), account_id: this._acctId('fn') }),
                API.getPins().catch(() => ({ pins: [] })),
                API.getGoals().catch(() => ({ goals: [] })),
            ]);
            const fnPins = (pins.pins || []).filter(p => p.platform === 'fn');
            const fnGoals = (goals.goals || []).filter(g => g.platform === 'fn' || g.platform === 'all');
            const fnHealth = window.PlatformHealth && window.PlatformHealth.get('fn');
            const isUnconfigured = fnHealth && fnHealth.configured === false;
            if (isUnconfigured || (summary.total_submissions || 0) === 0) {
                this._setContent(`
                    ${this._refreshIndicatorHtml()}
                    <div class="page-header"><h2>FurryNetwork Dashboard</h2></div>
                    ${Components.platformEmptyState('fn', isUnconfigured ? {} : { reason: 'FurryNetwork is configured but no submissions have been polled yet. The first poll may still be running.' })}
                `);
                return;
            }
            const html = `
                ${this._refreshIndicatorHtml()}
                <div class="page-header">
                    <h2>FurryNetwork Dashboard</h2>
                    <div style="display:flex;gap:8px">
                        <button class="btn btn-primary" data-poll="fn">Poll Now</button>
                        <button class="btn btn-secondary" data-resync="fn">Full Resync</button>
                        <button class="btn btn-secondary" data-export="fn">Export CSV</button>
                    </div>
                </div>
                ${fnPins.length ? Components.pinnedSubmissions(fnPins, 'fn') : ''}
                ${fnGoals.length ? `<div class="goals-section"><h3>Goals</h3>${Components.goalProgressCards(fnGoals)}</div>` : ''}
                <div class="stats-grid">
                    ${Components.statCard('Total Submissions', summary.total_submissions, null, '#/fn/submissions')}
                    ${Components.statCard('Total Views', summary.total_views || 0)}
                    ${Components.statCard('Total Favorites', summary.total_favorites || 0)}
                    ${Components.statCard('Total Comments', summary.total_comments || 0)}
                </div>
                ${summary.growth_rates ? Components.growthRateCards(summary.growth_rates, { views: 'views/day', faves: 'faves/day', comments: 'comments/day' }) : ''}
                ${Components.dateRangeBar(this._dateRange)}
                <div class="chart-container">
                    <h3>Views Over Time (Aggregate)</h3>
                    <div class="chart-wrap"><canvas id="chart-agg-views"></canvas></div>
                </div>
                <div class="chart-row">
                    <div class="chart-container"><h3>Top Viewed</h3>${Components.fnTopList(summary.top_viewed, 'views', 'title', 'submission_id')}</div>
                    <div class="chart-container"><h3>Top Favorited</h3>${Components.fnTopList(summary.top_faved, 'favorites_count', 'title', 'submission_id')}</div>
                </div>
                <div class="chart-row">
                    <div class="chart-container"><h3>Fastest Growing (24h)</h3>${Components.fnTopList(summary.fastest_growing, 'views_gained', 'title', 'submission_id')}</div>
                </div>
            `;
            this._setContent(html);
            if (agg.snapshots && agg.snapshots.length > 0) {
                Charts.aggregateLine('chart-agg-views', agg.snapshots, ['views']);
            }
            this._bindDateRange(() => this.renderFNDashboard());
            this._bindPinAndGoalActions(() => this.renderFNDashboard());
            this._startAutoRefresh(() => this.renderFNDashboard());
        } catch (err) {
            this._setContent(`<div class="empty-state"><h3>Error loading FurryNetwork dashboard</h3><p>${Utils.escapeHtml(err.message)}</p></div>`);
        }
    },

    async renderFNSubmissions() {
        this._loading();
        try {
            const data = await API.getFNSubmissions({
                sort_by: this._fnSortState.field, order: this._fnSortState.order, account_id: this._acctId('fn'),
            });
            const gridRenderer = (subs) => Components.submissionCardGrid(subs, {
                idKey: 'submission_id', titleKey: 'title', thumbKey: 'thumbnail_url', proxyThumb: false,
                typeKey: 'content_type', typeLabels: Components.E621_TYPE_LABELS,
                detailRoute: '/fn/submission', dateKey: 'posted_at',
                stats: [
                    { key: 'views', deltaKey: 'views_delta', label: 'views' },
                    { key: 'favorites_count', deltaKey: 'favorites_delta', label: 'favorites' },
                    { key: 'comments_count', deltaKey: 'comments_delta', label: 'comments' },
                ],
            });
            const html = `
                ${this._refreshIndicatorHtml()}
                <div class="page-header"><h2>FurryNetwork Submissions</h2></div>
                <div class="toolbar">
                    <input type="text" class="search-input" id="search-input" placeholder="Search submissions...">
                    <select class="filter-select" id="fn-sort">
                        <option value="views" ${this._fnSortState.field === 'views' ? 'selected' : ''}>Most viewed</option>
                        <option value="favorites_count" ${this._fnSortState.field === 'favorites_count' ? 'selected' : ''}>Most favorited</option>
                        <option value="comments_count" ${this._fnSortState.field === 'comments_count' ? 'selected' : ''}>Most comments</option>
                        <option value="posted_at" ${this._fnSortState.field === 'posted_at' ? 'selected' : ''}>Newest</option>
                    </select>
                </div>
                <div id="grid-container">${gridRenderer(data.submissions)}</div>
            `;
            this._setContent(html);
            const sortSel = document.getElementById('fn-sort');
            if (sortSel) sortSel.addEventListener('change', () => { this._fnSortState.field = sortSel.value; this.renderFNSubmissions(); });
            const searchInput = document.getElementById('search-input');
            if (searchInput) searchInput.addEventListener('input', () => {
                const q = searchInput.value.toLowerCase();
                document.getElementById('grid-container').innerHTML =
                    gridRenderer(data.submissions.filter(s => (s.title || '').toLowerCase().includes(q)));
            });
            this._startAutoRefresh(() => this.renderFNSubmissions());
        } catch (err) {
            this._setContent(`<div class="empty-state"><h3>Error loading FurryNetwork submissions</h3><p>${Utils.escapeHtml(err.message)}</p></div>`);
        }
    },

    async renderFNDetail(postId) {
        this._loading();
        try {
            const [data, pins, allTags] = await Promise.all([
                API.getFNSubmission(postId),
                API.getPins().catch(() => ({ pins: [] })),
                API.getTags().catch(() => ({ tags: [] })),
            ]);
            const sub = data.submission;
            const fullId = sub.submission_id;
            const isPinned = (pins.pins || []).some(p => p.platform === 'fn' && String(p.submission_id) === String(fullId));
            const currentTags = sub.tags || [];
            const html = `
                ${this._refreshIndicatorHtml()}
                <a href="#/fn/submissions" class="back-link">&larr; Back to FurryNetwork Submissions</a>
                <div class="detail-header">
                    ${sub.thumbnail_url ? `<img class="detail-thumb" src="${Utils.escapeHtml(Utils.safeUrl(sub.thumbnail_url) || '')}" alt="" style="max-width:160px;border-radius:8px;margin-right:16px">` : ''}
                    <div class="detail-info">
                        <h2>${Utils.escapeHtml(sub.title)}</h2>
                        <div class="detail-meta">by ${Utils.escapeHtml(sub.username)} &middot; ${Utils.formatDate(sub.posted_at)}${sub.rating ? ' &middot; ' + Utils.escapeHtml(sub.rating) : ''}</div>
                        <div class="detail-meta"><a href="${Utils.escapeHtml(Utils.safeUrl(sub.link) || '#')}" target="_blank">View on FurryNetwork</a></div>
                        <div class="detail-stats">
                            <div class="detail-stat">${Utils.formatNumber(sub.views || 0)} <span class="lbl">views</span></div>
                            <div class="detail-stat">${Utils.formatNumber(sub.favorites_count || 0)} <span class="lbl">favorites</span></div>
                            <div class="detail-stat">${Utils.formatNumber(sub.comments_count || 0)} <span class="lbl">comments</span></div>
                        </div>
                        <div style="margin-top:8px;display:flex;gap:8px;align-items:center;flex-wrap:wrap">
                            <button class="btn ${isPinned ? 'btn-danger' : 'btn-secondary'} btn-pin" data-platform="fn" data-id="${Utils.escapeHtml(fullId)}" style="padding:4px 10px;font-size:12px">${isPinned ? 'Unpin' : 'Pin'}</button>
                            ${currentTags.map(t => Components.tagBadge(t)).join('')}
                            <button class="btn btn-secondary btn-add-tag" data-platform="fn" data-id="${Utils.escapeHtml(fullId)}" style="padding:4px 10px;font-size:12px">+ Tag</button>
                        </div>
                        <div style="margin-top:8px">${Components.keywords(sub.keywords)}</div>
                    </div>
                </div>
                ${Components.growthRateCards(data.growth_rates, { views: 'views/day', faves: 'faves/day', comments: 'comments/day' })}
                ${Components.dateRangeBar(this._dateRange)}
                <div class="chart-container"><h3>Stats Over Time</h3><div class="chart-wrap"><canvas id="chart-detail"></canvas></div></div>
            `;
            this._setContent(html);
            if (data.snapshots && data.snapshots.length > 0) {
                Charts.submissionLine('chart-detail', data.snapshots, ['views', 'favorites_count', 'comments_count']);
            }
            this._bindDateRange(async () => {
                const range = Utils.getDateRange(this._dateRange);
                const snaps = await API.getFNSnapshots(postId, range);
                Charts.submissionLine('chart-detail', snaps.snapshots, ['views', 'favorites_count', 'comments_count']);
            });
            this._bindDetailPinTag('fn', fullId, allTags.tags || [], () => this.renderFNDetail(postId));
            this._startAutoRefresh(() => this.renderFNDetail(postId));
        } catch (err) {
            this._setContent(`<div class="empty-state"><h3>Error loading FurryNetwork submission</h3><p>${Utils.escapeHtml(err.message)}</p></div>`);
        }
    },

    async renderFNCompare() {
        this._loading();
        try {
            const data = await API.getFNSubmissions({ sort_by: 'views', order: 'desc', account_id: this._acctId('fn') });
            const subs = data.submissions;
            const chips = subs.map(s => `
                <label class="compare-chip ${this._fnCompareIds.has(String(s.submission_id)) ? 'selected' : ''}" data-id="${Utils.escapeHtml(String(s.submission_id))}">
                    <input type="checkbox" ${this._fnCompareIds.has(String(s.submission_id)) ? 'checked' : ''}>
                    ${Utils.escapeHtml(Utils.truncate(s.title, 25))}
                </label>`).join('');
            const html = `
                ${this._refreshIndicatorHtml()}
                <div class="page-header">
                    <h2>Compare FurryNetwork Submissions</h2>
                    <div>
                        <select class="filter-select" id="compare-metric">
                            <option value="views" ${this._fnCompareMetric === 'views' ? 'selected' : ''}>Views</option>
                            <option value="favorites_count" ${this._fnCompareMetric === 'favorites_count' ? 'selected' : ''}>Favorites</option>
                            <option value="comments_count" ${this._fnCompareMetric === 'comments_count' ? 'selected' : ''}>Comments</option>
                        </select>
                    </div>
                </div>
                <p style="font-size:13px;color:var(--text-muted);margin-bottom:12px">Select 2-5 submissions to compare their trends over time.</p>
                <div class="compare-select">${chips}</div>
                ${Components.dateRangeBar(this._dateRange)}
                <div class="chart-container" id="compare-chart-container" style="${this._fnCompareIds.size < 2 ? 'display:none' : ''}">
                    <h3>Comparison</h3><div class="chart-wrap"><canvas id="chart-compare"></canvas></div>
                </div>
                ${this._fnCompareIds.size < 2 ? '<div class="empty-state"><p>Select at least 2 submissions above to see their trends compared.</p></div>' : ''}
            `;
            this._setContent(html);
            document.querySelectorAll('.compare-chip').forEach(chip => {
                chip.addEventListener('click', (e) => {
                    e.preventDefault();
                    const id = chip.dataset.id;
                    if (this._fnCompareIds.has(id)) this._fnCompareIds.delete(id);
                    else if (this._fnCompareIds.size < 5) this._fnCompareIds.add(id);
                    this.renderFNCompare();
                });
            });
            const metricSelect = document.getElementById('compare-metric');
            if (metricSelect) metricSelect.addEventListener('change', () => { this._fnCompareMetric = metricSelect.value; this._loadFNComparisonChart(); });
            this._bindDateRange(() => this._loadFNComparisonChart());
            if (this._fnCompareIds.size >= 2) await this._loadFNComparisonChart();
            this._startAutoRefresh(() => this.renderFNCompare());
        } catch (err) {
            this._setContent(`<div class="empty-state"><h3>Error</h3><p>${Utils.escapeHtml(err.message)}</p></div>`);
        }
    },

    async _loadFNComparisonChart() {
        try {
            if (this._fnCompareIds.size < 2) return;
            const range = Utils.getDateRange(this._dateRange);
            const data = await API.getFNComparison([...this._fnCompareIds], range);
            const container = document.getElementById('compare-chart-container');
            if (container) container.style.display = '';
            Charts.comparisonLine('chart-compare', data.series, data.titles, this._fnCompareMetric);
        } catch (e) {
            console.error('Failed to load FN comparison chart:', e);
        }
    },

    async renderE621Dashboard() {
        this._loading();
        try {
            const [summary, agg, pins, goals] = await Promise.all([
                API.getE621Summary({ account_id: this._acctId('e621') }),
                API.getE621Aggregate({ ...Utils.getDateRange(this._dateRange), account_id: this._acctId('e621') }),
                API.getPins().catch(() => ({ pins: [] })),
                API.getGoals().catch(() => ({ goals: [] })),
            ]);
            const e621Pins = (pins.pins || []).filter(p => p.platform === 'e621');
            const e621Goals = (goals.goals || []).filter(g => g.platform === 'e621' || g.platform === 'all');

            const e621Health = window.PlatformHealth && window.PlatformHealth.get('e621');
            const isUnconfigured = e621Health && e621Health.configured === false;
            if (isUnconfigured || (summary.total_submissions || 0) === 0) {
                this._setContent(`
                    ${this._refreshIndicatorHtml()}
                    <div class="page-header"><h2>e621 Dashboard</h2></div>
                    ${Components.platformEmptyState('e621', isUnconfigured ? {} : { reason: 'e621 is configured but no posts have been polled yet. The first poll may still be running.' })}
                `);
                return;
            }

            const html = `
                ${this._refreshIndicatorHtml()}
                <div class="page-header">
                    <h2>e621 Dashboard</h2>
                    <div style="display:flex;gap:8px">
                        <button class="btn btn-primary" data-poll="e621">Poll Now</button>
                        <button class="btn btn-secondary" data-resync="e621">Full Resync</button>
                        <button class="btn btn-secondary" data-export="e621">Export CSV</button>
                    </div>
                </div>

                ${e621Pins.length ? Components.pinnedSubmissions(e621Pins, 'e621') : ''}
                ${e621Goals.length ? `<div class="goals-section"><h3>Goals</h3>${Components.goalProgressCards(e621Goals)}</div>` : ''}

                <div class="stats-grid">
                    ${Components.statCard('Total Posts', summary.total_submissions, null, '#/e621/submissions')}
                    ${Components.statCard('Total Score', summary.total_score || 0)}
                    ${Components.statCard('Total Favorites', summary.total_favorites || 0)}
                    ${Components.statCard('Total Comments', summary.total_comments || 0)}
                </div>

                ${summary.growth_rates ? Components.growthRateCards(summary.growth_rates, { views: 'score/day', faves: 'faves/day', comments: 'comments/day' }) : ''}

                ${Components.dateRangeBar(this._dateRange)}

                <div class="chart-container">
                    <h3>Score Over Time (Aggregate)</h3>
                    <div class="chart-wrap"><canvas id="chart-agg-views"></canvas></div>
                </div>

                <div class="chart-row">
                    <div class="chart-container">
                        <h3>Top Scored</h3>
                        ${Components.e621TopList(summary.top_scored, 'score', 'title', 'submission_id')}
                    </div>
                    <div class="chart-container">
                        <h3>Top Favorited</h3>
                        ${Components.e621TopList(summary.top_faved, 'favorites_count', 'title', 'submission_id')}
                    </div>
                </div>

                <div class="chart-row">
                    <div class="chart-container">
                        <h3>Fastest Growing (24h)</h3>
                        ${Components.e621TopList(summary.fastest_growing, 'score_gained', 'title', 'submission_id')}
                    </div>
                </div>
            `;

            this._setContent(html);

            if (agg.snapshots && agg.snapshots.length > 0) {
                Charts.aggregateLine('chart-agg-views', agg.snapshots, ['score']);
            }

            this._bindDateRange(() => this.renderE621Dashboard());
            this._bindPinAndGoalActions(() => this.renderE621Dashboard());
            this._startAutoRefresh(() => this.renderE621Dashboard());
        } catch (err) {
            this._setContent(`<div class="empty-state"><h3>Error loading e621 dashboard</h3><p>${Utils.escapeHtml(err.message)}</p></div>`);
        }
    },

    // ── E621 Submissions ────────────────────────────────────────

    async renderE621Submissions() {
        this._loading();
        try {
            const data = await API.getE621Submissions({
                sort_by: this._e621SortState.field,
                order: this._e621SortState.order,
                account_id: this._acctId('e621'),
            });

            const _vm = localStorage.getItem('pp-view-mode') || 'grid';
            // e621 CDN images are hotlinkable — use the thumbnail URL directly.
            const e621GridRenderer = (subs) => Components.submissionCardGrid(
                subs,
                {
                    idKey: 'submission_id', titleKey: 'title', thumbKey: 'thumbnail_url', proxyThumb: false,
                    typeKey: 'content_type', typeLabels: Components.E621_TYPE_LABELS,
                    detailRoute: '/e621/submission', dateKey: 'posted_at',
                    stats: [
                        { key: 'score', deltaKey: 'score_delta', label: 'score' },
                        { key: 'favorites_count', deltaKey: 'favorites_delta', label: 'favorites' },
                        { key: 'comments_count', deltaKey: 'comments_delta', label: 'comments' },
                    ],
                }
            );
            const gridHtml = e621GridRenderer(data.submissions);
            const html = `
                ${this._refreshIndicatorHtml()}
                <div class="page-header"><h2>e621 Posts</h2></div>
                <div class="toolbar">
                    <input type="text" class="search-input" id="search-input" placeholder="Search posts...">
                    <div class="view-toggle">
                        <button class="view-toggle-btn ${_vm === 'grid' ? 'active' : ''}" data-view="grid" title="Grid view">&#9638;</button>
                        <button class="view-toggle-btn ${_vm === 'list' ? 'active' : ''}" data-view="list" title="List view">&#9776;</button>
                    </div>
                </div>
                <div id="grid-container" style="${_vm !== 'grid' ? 'display:none' : ''}">${gridHtml}</div>
                <div id="table-container" class="table-scroll" style="${_vm !== 'list' ? 'display:none' : ''}">
                    ${Components.e621SubmissionsTable(data.submissions)}
                </div>
            `;

            this._setContent(html);
            this._bindViewToggle();
            this._bindE621TableSort();
            this._bindE621Search(data.submissions, e621GridRenderer);
            this._startAutoRefresh(() => this.renderE621Submissions());
        } catch (err) {
            this._setContent(`<div class="empty-state"><h3>Error loading e621 posts</h3><p>${Utils.escapeHtml(err.message)}</p></div>`);
        }
    },

    // ── E621 Submission Detail ──────────────────────────────────

    async renderE621Detail(postId) {
        this._loading();
        try {
            const [data, pins, allTags] = await Promise.all([
                API.getE621Submission(postId),
                API.getPins().catch(() => ({ pins: [] })),
                API.getTags().catch(() => ({ tags: [] })),
            ]);
            const sub = data.submission;
            const fullId = sub.submission_id;
            const isPinned = (pins.pins || []).some(p => p.platform === 'e621' && String(p.submission_id) === String(fullId));
            const currentTags = sub.tags || [];

            const html = `
                ${this._refreshIndicatorHtml()}
                <a href="#/e621/submissions" class="back-link">&larr; Back to e621 Posts</a>
                <div class="detail-header">
                    ${sub.thumbnail_url ? `<img class="detail-thumb" src="${Utils.escapeHtml(Utils.safeUrl(sub.thumbnail_url) || '')}" alt="" style="max-width:160px;border-radius:8px;margin-right:16px">` : ''}
                    <div class="detail-info">
                        <h2>${Utils.escapeHtml(sub.title)}</h2>
                        <div class="detail-meta">by ${Utils.escapeHtml(sub.username)} &middot; ${Utils.formatDate(sub.posted_at)} &middot; ${Utils.escapeHtml(Components.E621_TYPE_LABELS[sub.content_type] || sub.content_type || 'Image')}${sub.rating ? ' &middot; ' + Utils.escapeHtml(sub.rating) : ''}</div>
                        <div class="detail-meta"><a href="${Utils.escapeHtml(Utils.safeUrl(sub.link) || '#')}" target="_blank">View on e621</a></div>
                        <div class="detail-stats">
                            <div class="detail-stat">${Utils.formatNumber(sub.score || 0)} <span class="lbl">score</span></div>
                            <div class="detail-stat">${Utils.formatNumber(sub.favorites_count || 0)} <span class="lbl">favorites</span></div>
                            <div class="detail-stat">${Utils.formatNumber(sub.comments_count || 0)} <span class="lbl">comments</span></div>
                        </div>
                        <div style="margin-top:8px;display:flex;gap:8px;align-items:center;flex-wrap:wrap">
                            <button class="btn ${isPinned ? 'btn-danger' : 'btn-secondary'} btn-pin" data-platform="e621" data-id="${Utils.escapeHtml(fullId)}" style="padding:4px 10px;font-size:12px">${isPinned ? 'Unpin' : 'Pin'}</button>
                            ${currentTags.map(t => Components.tagBadge(t)).join('')}
                            <button class="btn btn-secondary btn-add-tag" data-platform="e621" data-id="${Utils.escapeHtml(fullId)}" style="padding:4px 10px;font-size:12px">+ Tag</button>
                        </div>
                        <div style="margin-top:8px">${Components.keywords(sub.keywords)}</div>
                    </div>
                </div>

                ${Components.growthRateCards(data.growth_rates, { views: 'score/day', faves: 'faves/day', comments: 'comments/day' })}

                ${Components.dateRangeBar(this._dateRange)}

                <div class="chart-container">
                    <h3>Stats Over Time</h3>
                    <div class="chart-wrap"><canvas id="chart-detail"></canvas></div>
                </div>
            `;

            this._setContent(html);

            if (data.snapshots && data.snapshots.length > 0) {
                Charts.submissionLine('chart-detail', data.snapshots, ['score', 'favorites_count', 'comments_count']);
            }

            this._bindDateRange(async () => {
                const range = Utils.getDateRange(this._dateRange);
                const snaps = await API.getE621Snapshots(postId, range);
                Charts.submissionLine('chart-detail', snaps.snapshots, ['score', 'favorites_count', 'comments_count']);
            });

            this._bindDetailPinTag('e621', fullId, allTags.tags || [], () => this.renderE621Detail(postId));
            this._startAutoRefresh(() => this.renderE621Detail(postId));
        } catch (err) {
            this._setContent(`<div class="empty-state"><h3>Error loading e621 post</h3><p>${Utils.escapeHtml(err.message)}</p></div>`);
        }
    },

    // ── E621 Compare ────────────────────────────────────────────

    async renderE621Compare() {
        this._loading();
        try {
            const data = await API.getE621Submissions({ sort_by: 'score', order: 'desc', account_id: this._acctId('e621') });
            const subs = data.submissions;

            const chips = subs.map(s => `
                <label class="compare-chip ${this._e621CompareIds.has(String(s.submission_id)) ? 'selected' : ''}" data-id="${Utils.escapeHtml(String(s.submission_id))}">
                    <input type="checkbox" ${this._e621CompareIds.has(String(s.submission_id)) ? 'checked' : ''}>
                    ${Utils.escapeHtml(Utils.truncate(s.title, 25))}
                </label>
            `).join('');

            const html = `
                ${this._refreshIndicatorHtml()}
                <div class="page-header">
                    <h2>Compare e621 Posts</h2>
                    <div>
                        <select class="filter-select" id="compare-metric">
                            <option value="score" ${this._e621CompareMetric === 'score' ? 'selected' : ''}>Score</option>
                            <option value="favorites_count" ${this._e621CompareMetric === 'favorites_count' ? 'selected' : ''}>Favorites</option>
                            <option value="comments_count" ${this._e621CompareMetric === 'comments_count' ? 'selected' : ''}>Comments</option>
                        </select>
                    </div>
                </div>
                <p style="font-size:13px;color:var(--text-muted);margin-bottom:12px">Select 2-5 e621 posts to compare their trends over time.</p>
                <div class="compare-select">${chips}</div>

                ${Components.dateRangeBar(this._dateRange)}

                <div class="chart-container" id="compare-chart-container" style="${this._e621CompareIds.size < 2 ? 'display:none' : ''}">
                    <h3>Comparison</h3>
                    <div class="chart-wrap"><canvas id="chart-compare"></canvas></div>
                </div>
                ${this._e621CompareIds.size < 2 ? '<div class="empty-state"><p>Select at least 2 posts above to see their trends compared.</p></div>' : ''}
            `;

            this._setContent(html);

            document.querySelectorAll('.compare-chip').forEach(chip => {
                chip.addEventListener('click', (e) => {
                    e.preventDefault();
                    const id = chip.dataset.id;
                    if (this._e621CompareIds.has(id)) {
                        this._e621CompareIds.delete(id);
                    } else if (this._e621CompareIds.size < 5) {
                        this._e621CompareIds.add(id);
                    }
                    this.renderE621Compare();
                });
            });

            const metricSelect = document.getElementById('compare-metric');
            if (metricSelect) {
                metricSelect.addEventListener('change', () => {
                    this._e621CompareMetric = metricSelect.value;
                    this._loadE621ComparisonChart();
                });
            }

            this._bindDateRange(() => this._loadE621ComparisonChart());

            if (this._e621CompareIds.size >= 2) {
                await this._loadE621ComparisonChart();
            }

            this._startAutoRefresh(() => this.renderE621Compare());
        } catch (err) {
            this._setContent(`<div class="empty-state"><h3>Error</h3><p>${Utils.escapeHtml(err.message)}</p></div>`);
        }
    },

    async _loadE621ComparisonChart() {
        try {
            if (this._e621CompareIds.size < 2) return;
            const range = Utils.getDateRange(this._dateRange);
            const data = await API.getE621Comparison([...this._e621CompareIds], range);
            const container = document.getElementById('compare-chart-container');
            if (container) container.style.display = '';
            Charts.comparisonLine('chart-compare', data.series, data.titles, this._e621CompareMetric);
        } catch (e) {
            console.error('Failed to load e621 comparison chart:', e);
        }
    },

    _bindE621TableSort() {
        document.querySelectorAll('#e621-submissions-table th[data-sort]').forEach(th => {
            th.addEventListener('click', () => {
                const field = th.dataset.sort;
                if (this._e621SortState.field === field) {
                    this._e621SortState.order = this._e621SortState.order === 'desc' ? 'asc' : 'desc';
                } else {
                    this._e621SortState.field = field;
                    this._e621SortState.order = 'desc';
                }
                this.renderE621Submissions();
            });
        });
    },

    // e621 variant of _bindSearch(). Filters by text (title only).
    _bindE621Search(allSubmissions, gridRenderer) {
        const input = document.getElementById('search-input');
        const doFilter = () => {
            const q = (input?.value || '').toLowerCase();
            let filtered = allSubmissions;
            if (q) {
                filtered = filtered.filter(s => (s.title || '').toLowerCase().includes(q));
            }
            const grid = document.getElementById('grid-container');
            if (grid && gridRenderer) grid.innerHTML = gridRenderer(filtered);
            document.getElementById('table-container').innerHTML = Components.e621SubmissionsTable(filtered);
            this._bindE621TableSort();
        };
        input?.addEventListener('input', doFilter);
    },

    async renderFBRDashboard() {
        this._loading();
        try {
            const [summary, agg, pins, goals] = await Promise.all([
                API.getFBRSummary({ account_id: this._acctId('fbr') }),
                API.getFBRAggregate({ ...Utils.getDateRange(this._dateRange), account_id: this._acctId('fbr') }),
                API.getPins().catch(() => ({ pins: [] })),
                API.getGoals().catch(() => ({ goals: [] })),
            ]);
            const fbrPins = (pins.pins || []).filter(p => p.platform === 'fbr');
            const fbrGoals = (goals.goals || []).filter(g => g.platform === 'fbr' || g.platform === 'all');

            const fbrHealth = window.PlatformHealth && window.PlatformHealth.get('fbr');
            const isUnconfigured = fbrHealth && fbrHealth.configured === false;
            if (isUnconfigured || (summary.total_submissions || 0) === 0) {
                this._setContent(`
                    ${this._refreshIndicatorHtml()}
                    <div class="page-header"><h2>Furbooru Dashboard</h2></div>
                    ${Components.platformEmptyState('fbr', isUnconfigured ? {} : { reason: 'Furbooru is configured but no posts have been polled yet. The first poll may still be running.' })}
                `);
                return;
            }

            const html = `
                ${this._refreshIndicatorHtml()}
                <div class="page-header">
                    <h2>Furbooru Dashboard</h2>
                    <div style="display:flex;gap:8px">
                        <button class="btn btn-primary" data-poll="fbr">Poll Now</button>
                        <button class="btn btn-secondary" data-resync="fbr">Full Resync</button>
                        <button class="btn btn-secondary" data-export="fbr">Export CSV</button>
                    </div>
                </div>

                ${fbrPins.length ? Components.pinnedSubmissions(fbrPins, 'fbr') : ''}
                ${fbrGoals.length ? `<div class="goals-section"><h3>Goals</h3>${Components.goalProgressCards(fbrGoals)}</div>` : ''}

                <div class="stats-grid">
                    ${Components.statCard('Total Posts', summary.total_submissions, null, '#/fbr/submissions')}
                    ${Components.statCard('Total Score', summary.total_score || 0)}
                    ${Components.statCard('Total Favorites', summary.total_favorites || 0)}
                    ${Components.statCard('Total Comments', summary.total_comments || 0)}
                </div>

                ${summary.growth_rates ? Components.growthRateCards(summary.growth_rates, { views: 'score/day', faves: 'faves/day', comments: 'comments/day' }) : ''}

                ${Components.dateRangeBar(this._dateRange)}

                <div class="chart-container">
                    <h3>Score Over Time (Aggregate)</h3>
                    <div class="chart-wrap"><canvas id="chart-agg-views"></canvas></div>
                </div>

                <div class="chart-row">
                    <div class="chart-container">
                        <h3>Top Scored</h3>
                        ${Components.fbrTopList(summary.top_scored, 'score', 'title', 'submission_id')}
                    </div>
                    <div class="chart-container">
                        <h3>Top Favorited</h3>
                        ${Components.fbrTopList(summary.top_faved, 'favorites_count', 'title', 'submission_id')}
                    </div>
                </div>

                <div class="chart-row">
                    <div class="chart-container">
                        <h3>Fastest Growing (24h)</h3>
                        ${Components.fbrTopList(summary.fastest_growing, 'score_gained', 'title', 'submission_id')}
                    </div>
                </div>
            `;

            this._setContent(html);

            if (agg.snapshots && agg.snapshots.length > 0) {
                Charts.aggregateLine('chart-agg-views', agg.snapshots, ['score']);
            }

            this._bindDateRange(() => this.renderFBRDashboard());
            this._bindPinAndGoalActions(() => this.renderFBRDashboard());
            this._startAutoRefresh(() => this.renderFBRDashboard());
        } catch (err) {
            this._setContent(`<div class="empty-state"><h3>Error loading Furbooru dashboard</h3><p>${Utils.escapeHtml(err.message)}</p></div>`);
        }
    },

    // ── Furbooru Submissions ────────────────────────────────────────

    async renderFBRSubmissions() {
        this._loading();
        try {
            const data = await API.getFBRSubmissions({
                sort_by: this._fbrSortState.field,
                order: this._fbrSortState.order,
                account_id: this._acctId('fbr'),
            });

            const _vm = localStorage.getItem('pp-view-mode') || 'grid';
            // Furbooru (Philomena) CDN images are hotlinkable — use the thumb URL directly.
            const fbrGridRenderer = (subs) => Components.submissionCardGrid(
                subs,
                {
                    idKey: 'submission_id', titleKey: 'title', thumbKey: 'thumbnail_url', proxyThumb: false,
                    typeKey: 'content_type', typeLabels: Components.E621_TYPE_LABELS,
                    detailRoute: '/fbr/submission', dateKey: 'posted_at',
                    stats: [
                        { key: 'score', deltaKey: 'score_delta', label: 'score' },
                        { key: 'favorites_count', deltaKey: 'favorites_delta', label: 'favorites' },
                        { key: 'comments_count', deltaKey: 'comments_delta', label: 'comments' },
                    ],
                }
            );
            const gridHtml = fbrGridRenderer(data.submissions);
            const html = `
                ${this._refreshIndicatorHtml()}
                <div class="page-header"><h2>Furbooru Posts</h2></div>
                <div class="toolbar">
                    <input type="text" class="search-input" id="search-input" placeholder="Search posts...">
                    <div class="view-toggle">
                        <button class="view-toggle-btn ${_vm === 'grid' ? 'active' : ''}" data-view="grid" title="Grid view">&#9638;</button>
                        <button class="view-toggle-btn ${_vm === 'list' ? 'active' : ''}" data-view="list" title="List view">&#9776;</button>
                    </div>
                </div>
                <div id="grid-container" style="${_vm !== 'grid' ? 'display:none' : ''}">${gridHtml}</div>
                <div id="table-container" class="table-scroll" style="${_vm !== 'list' ? 'display:none' : ''}">
                    ${Components.fbrSubmissionsTable(data.submissions)}
                </div>
            `;

            this._setContent(html);
            this._bindViewToggle();
            this._bindFBRTableSort();
            this._bindFBRSearch(data.submissions, fbrGridRenderer);
            this._startAutoRefresh(() => this.renderFBRSubmissions());
        } catch (err) {
            this._setContent(`<div class="empty-state"><h3>Error loading Furbooru posts</h3><p>${Utils.escapeHtml(err.message)}</p></div>`);
        }
    },

    // ── Furbooru Submission Detail ──────────────────────────────────

    async renderFBRDetail(postId) {
        this._loading();
        try {
            const [data, pins, allTags] = await Promise.all([
                API.getFBRSubmission(postId),
                API.getPins().catch(() => ({ pins: [] })),
                API.getTags().catch(() => ({ tags: [] })),
            ]);
            const sub = data.submission;
            const fullId = sub.submission_id;
            const isPinned = (pins.pins || []).some(p => p.platform === 'fbr' && String(p.submission_id) === String(fullId));
            const currentTags = sub.tags || [];

            const html = `
                ${this._refreshIndicatorHtml()}
                <a href="#/fbr/submissions" class="back-link">&larr; Back to Furbooru Posts</a>
                <div class="detail-header">
                    ${sub.thumbnail_url ? `<img class="detail-thumb" src="${Utils.escapeHtml(Utils.safeUrl(sub.thumbnail_url) || '')}" alt="" style="max-width:160px;border-radius:8px;margin-right:16px">` : ''}
                    <div class="detail-info">
                        <h2>${Utils.escapeHtml(sub.title)}</h2>
                        <div class="detail-meta">by ${Utils.escapeHtml(sub.username)} &middot; ${Utils.formatDate(sub.posted_at)} &middot; ${Utils.escapeHtml(Components.E621_TYPE_LABELS[sub.content_type] || sub.content_type || 'Image')}${sub.rating ? ' &middot; ' + Utils.escapeHtml(sub.rating) : ''}</div>
                        <div class="detail-meta"><a href="${Utils.escapeHtml(Utils.safeUrl(sub.link) || '#')}" target="_blank">View on Furbooru</a></div>
                        <div class="detail-stats">
                            <div class="detail-stat">${Utils.formatNumber(sub.score || 0)} <span class="lbl">score</span></div>
                            <div class="detail-stat">${Utils.formatNumber(sub.favorites_count || 0)} <span class="lbl">favorites</span></div>
                            <div class="detail-stat">${Utils.formatNumber(sub.comments_count || 0)} <span class="lbl">comments</span></div>
                        </div>
                        <div style="margin-top:8px;display:flex;gap:8px;align-items:center;flex-wrap:wrap">
                            <button class="btn ${isPinned ? 'btn-danger' : 'btn-secondary'} btn-pin" data-platform="fbr" data-id="${Utils.escapeHtml(fullId)}" style="padding:4px 10px;font-size:12px">${isPinned ? 'Unpin' : 'Pin'}</button>
                            ${currentTags.map(t => Components.tagBadge(t)).join('')}
                            <button class="btn btn-secondary btn-add-tag" data-platform="fbr" data-id="${Utils.escapeHtml(fullId)}" style="padding:4px 10px;font-size:12px">+ Tag</button>
                        </div>
                        <div style="margin-top:8px">${Components.keywords(sub.keywords)}</div>
                    </div>
                </div>

                ${Components.growthRateCards(data.growth_rates, { views: 'score/day', faves: 'faves/day', comments: 'comments/day' })}

                ${Components.dateRangeBar(this._dateRange)}

                <div class="chart-container">
                    <h3>Stats Over Time</h3>
                    <div class="chart-wrap"><canvas id="chart-detail"></canvas></div>
                </div>
            `;

            this._setContent(html);

            if (data.snapshots && data.snapshots.length > 0) {
                Charts.submissionLine('chart-detail', data.snapshots, ['score', 'favorites_count', 'comments_count']);
            }

            this._bindDateRange(async () => {
                const range = Utils.getDateRange(this._dateRange);
                const snaps = await API.getFBRSnapshots(postId, range);
                Charts.submissionLine('chart-detail', snaps.snapshots, ['score', 'favorites_count', 'comments_count']);
            });

            this._bindDetailPinTag('fbr', fullId, allTags.tags || [], () => this.renderFBRDetail(postId));
            this._startAutoRefresh(() => this.renderFBRDetail(postId));
        } catch (err) {
            this._setContent(`<div class="empty-state"><h3>Error loading Furbooru post</h3><p>${Utils.escapeHtml(err.message)}</p></div>`);
        }
    },

    // ── Furbooru Compare ────────────────────────────────────────────

    async renderFBRCompare() {
        this._loading();
        try {
            const data = await API.getFBRSubmissions({ sort_by: 'score', order: 'desc', account_id: this._acctId('fbr') });
            const subs = data.submissions;

            const chips = subs.map(s => `
                <label class="compare-chip ${this._fbrCompareIds.has(String(s.submission_id)) ? 'selected' : ''}" data-id="${Utils.escapeHtml(String(s.submission_id))}">
                    <input type="checkbox" ${this._fbrCompareIds.has(String(s.submission_id)) ? 'checked' : ''}>
                    ${Utils.escapeHtml(Utils.truncate(s.title, 25))}
                </label>
            `).join('');

            const html = `
                ${this._refreshIndicatorHtml()}
                <div class="page-header">
                    <h2>Compare Furbooru Posts</h2>
                    <div>
                        <select class="filter-select" id="compare-metric">
                            <option value="score" ${this._fbrCompareMetric === 'score' ? 'selected' : ''}>Score</option>
                            <option value="favorites_count" ${this._fbrCompareMetric === 'favorites_count' ? 'selected' : ''}>Favorites</option>
                            <option value="comments_count" ${this._fbrCompareMetric === 'comments_count' ? 'selected' : ''}>Comments</option>
                        </select>
                    </div>
                </div>
                <p style="font-size:13px;color:var(--text-muted);margin-bottom:12px">Select 2-5 Furbooru posts to compare their trends over time.</p>
                <div class="compare-select">${chips}</div>

                ${Components.dateRangeBar(this._dateRange)}

                <div class="chart-container" id="compare-chart-container" style="${this._fbrCompareIds.size < 2 ? 'display:none' : ''}">
                    <h3>Comparison</h3>
                    <div class="chart-wrap"><canvas id="chart-compare"></canvas></div>
                </div>
                ${this._fbrCompareIds.size < 2 ? '<div class="empty-state"><p>Select at least 2 posts above to see their trends compared.</p></div>' : ''}
            `;

            this._setContent(html);

            document.querySelectorAll('.compare-chip').forEach(chip => {
                chip.addEventListener('click', (e) => {
                    e.preventDefault();
                    const id = chip.dataset.id;
                    if (this._fbrCompareIds.has(id)) {
                        this._fbrCompareIds.delete(id);
                    } else if (this._fbrCompareIds.size < 5) {
                        this._fbrCompareIds.add(id);
                    }
                    this.renderFBRCompare();
                });
            });

            const metricSelect = document.getElementById('compare-metric');
            if (metricSelect) {
                metricSelect.addEventListener('change', () => {
                    this._fbrCompareMetric = metricSelect.value;
                    this._loadFBRComparisonChart();
                });
            }

            this._bindDateRange(() => this._loadFBRComparisonChart());

            if (this._fbrCompareIds.size >= 2) {
                await this._loadFBRComparisonChart();
            }

            this._startAutoRefresh(() => this.renderFBRCompare());
        } catch (err) {
            this._setContent(`<div class="empty-state"><h3>Error</h3><p>${Utils.escapeHtml(err.message)}</p></div>`);
        }
    },

    async _loadFBRComparisonChart() {
        try {
            if (this._fbrCompareIds.size < 2) return;
            const range = Utils.getDateRange(this._dateRange);
            const data = await API.getFBRComparison([...this._fbrCompareIds], range);
            const container = document.getElementById('compare-chart-container');
            if (container) container.style.display = '';
            Charts.comparisonLine('chart-compare', data.series, data.titles, this._fbrCompareMetric);
        } catch (e) {
            console.error('Failed to load Furbooru comparison chart:', e);
        }
    },

    _bindFBRTableSort() {
        document.querySelectorAll('#fbr-submissions-table th[data-sort]').forEach(th => {
            th.addEventListener('click', () => {
                const field = th.dataset.sort;
                if (this._fbrSortState.field === field) {
                    this._fbrSortState.order = this._fbrSortState.order === 'desc' ? 'asc' : 'desc';
                } else {
                    this._fbrSortState.field = field;
                    this._fbrSortState.order = 'desc';
                }
                this.renderFBRSubmissions();
            });
        });
    },

    // Furbooru variant of _bindSearch(). Filters by text (title only).
    _bindFBRSearch(allSubmissions, gridRenderer) {
        const input = document.getElementById('search-input');
        const doFilter = () => {
            const q = (input?.value || '').toLowerCase();
            let filtered = allSubmissions;
            if (q) {
                filtered = filtered.filter(s => (s.title || '').toLowerCase().includes(q));
            }
            const grid = document.getElementById('grid-container');
            if (grid && gridRenderer) grid.innerHTML = gridRenderer(filtered);
            document.getElementById('table-container').innerHTML = Components.fbrSubmissionsTable(filtered);
            this._bindFBRTableSort();
        };
        input?.addEventListener('input', doFilter);
    },

    // ── THR Dashboard ─────────────────────────────────────────
    // Threads dashboard with Views, Likes, Reposts, Replies (+ Quotes).

    async renderTHRDashboard() {
        this._loading();
        try {
            const [summary, agg, pins, goals] = await Promise.all([
                API.getTHRSummary({ account_id: this._acctId('thr') }),
                API.getTHRAggregate({ ...Utils.getDateRange(this._dateRange), account_id: this._acctId('thr') }),
                API.getPins().catch(() => ({ pins: [] })),
                API.getGoals().catch(() => ({ goals: [] })),
            ]);
            const thrPins = (pins.pins || []).filter(p => p.platform === 'thr');
            const thrGoals = (goals.goals || []).filter(g => g.platform === 'thr' || g.platform === 'all');

            const thrHealth = window.PlatformHealth && window.PlatformHealth.get('thr');
            const isUnconfigured = thrHealth && thrHealth.configured === false;
            if (isUnconfigured || (summary.total_submissions || 0) === 0) {
                this._setContent(`
                    ${this._refreshIndicatorHtml()}
                    <div class="page-header"><h2>Threads Dashboard</h2></div>
                    ${Components.platformEmptyState('thr', isUnconfigured ? {} : { reason: 'Threads is configured but no posts have been polled yet. The first poll may still be running.' })}
                `);
                return;
            }

            const html = `
                ${this._refreshIndicatorHtml()}
                <div class="page-header">
                    <h2>Threads Dashboard</h2>
                    <div style="display:flex;gap:8px">
                        <button class="btn btn-primary" data-poll="thr">Poll Now</button>
                        <button class="btn btn-secondary" data-resync="thr">Full Resync</button>
                        <button class="btn btn-secondary" data-export="thr">Export CSV</button>
                    </div>
                </div>

                ${thrPins.length ? Components.pinnedSubmissions(thrPins, 'thr') : ''}
                ${thrGoals.length ? `<div class="goals-section"><h3>Goals</h3>${Components.goalProgressCards(thrGoals)}</div>` : ''}

                <div class="stats-grid">
                    ${Components.statCard('Total Posts', summary.total_submissions, null, '#/thr/submissions')}
                    ${Components.statCard('Total Views', summary.total_views || 0)}
                    ${Components.statCard('Total Likes', summary.total_likes || 0)}
                    ${Components.statCard('Total Reposts', summary.total_reposts || 0)}
                    ${Components.statCard('Total Replies', summary.total_replies || 0)}
                </div>

                ${summary.growth_rates ? Components.growthRateCards(summary.growth_rates, { views: 'views/day', faves: 'likes/day', comments: 'replies/day' }) : ''}

                ${Components.dateRangeBar(this._dateRange)}

                <div class="chart-container">
                    <h3>Views Over Time (Aggregate)</h3>
                    <div class="chart-wrap"><canvas id="chart-agg-views"></canvas></div>
                </div>

                <div class="chart-row">
                    <div class="chart-container">
                        <h3>Top Viewed</h3>
                        ${Components.thrTopList(summary.top_viewed, 'views', 'title', 'submission_id')}
                    </div>
                    <div class="chart-container">
                        <h3>Top Liked</h3>
                        ${Components.thrTopList(summary.top_liked, 'likes', 'title', 'submission_id')}
                    </div>
                </div>

                <div class="chart-row">
                    <div class="chart-container">
                        <h3>Fastest Growing (24h)</h3>
                        ${Components.thrTopList(summary.fastest_growing, 'views_gained', 'title', 'submission_id')}
                    </div>
                </div>
            `;

            this._setContent(html);

            if (agg.snapshots && agg.snapshots.length > 0) {
                Charts.aggregateLine('chart-agg-views', agg.snapshots, ['views']);
            }

            this._bindDateRange(() => this.renderTHRDashboard());
            this._bindPinAndGoalActions(() => this.renderTHRDashboard());
            this._startAutoRefresh(() => this.renderTHRDashboard());
        } catch (err) {
            this._setContent(`<div class="empty-state"><h3>Error loading THR dashboard</h3><p>${Utils.escapeHtml(err.message)}</p></div>`);
        }
    },

    // ── THR Submissions ─────────────────────────────────────────

    async renderTHRSubmissions() {
        this._loading();
        try {
            const data = await API.getTHRSubmissions({
                sort_by: this._thrSortState.field,
                order: this._thrSortState.order,
                account_id: this._acctId('thr'),
            });

            const _vm = localStorage.getItem('pp-view-mode') || 'grid';
            const thrGridRenderer = (subs) => Components.submissionCardGrid(
                subs,
                {
                    idKey: 'submission_id', titleKey: 'title', thumbKey: 'thumbnail_url', proxyThumb: false,
                    typeKey: 'content_type', typeLabels: Components.THR_TYPE_LABELS,
                    detailRoute: '/thr/submission', dateKey: 'posted_at',
                    stats: [
                        { key: 'views', deltaKey: 'views_delta', label: 'views' },
                        { key: 'likes', deltaKey: 'likes_delta', label: 'likes' },
                        { key: 'replies', deltaKey: 'replies_delta', label: 'replies' },
                    ],
                }
            );
            const gridHtml = thrGridRenderer(data.submissions);
            const html = `
                ${this._refreshIndicatorHtml()}
                <div class="page-header"><h2>Threads Posts</h2></div>
                <div class="toolbar">
                    <input type="text" class="search-input" id="search-input" placeholder="Search posts...">
                    <div class="view-toggle">
                        <button class="view-toggle-btn ${_vm === 'grid' ? 'active' : ''}" data-view="grid" title="Grid view">&#9638;</button>
                        <button class="view-toggle-btn ${_vm === 'list' ? 'active' : ''}" data-view="list" title="List view">&#9776;</button>
                    </div>
                </div>
                <div id="grid-container" style="${_vm !== 'grid' ? 'display:none' : ''}">${gridHtml}</div>
                <div id="table-container" class="table-scroll" style="${_vm !== 'list' ? 'display:none' : ''}">
                    ${Components.thrSubmissionsTable(data.submissions)}
                </div>
            `;

            this._setContent(html);
            this._bindViewToggle();
            this._bindTHRTableSort();
            this._bindTHRSearch(data.submissions, thrGridRenderer);
            this._startAutoRefresh(() => this.renderTHRSubmissions());
        } catch (err) {
            this._setContent(`<div class="empty-state"><h3>Error loading THR submissions</h3><p>${Utils.escapeHtml(err.message)}</p></div>`);
        }
    },

    // ── THR Submission Detail ───────────────────────────────────

    async renderTHRDetail(postId) {
        this._loading();
        try {
            const [data, pins, allTags] = await Promise.all([
                API.getTHRSubmission(postId),
                API.getPins().catch(() => ({ pins: [] })),
                API.getTags().catch(() => ({ tags: [] })),
            ]);
            const sub = data.submission;
            const fullId = sub.submission_id;
            const isPinned = (pins.pins || []).some(p => p.platform === 'thr' && String(p.submission_id) === String(fullId));
            const currentTags = sub.tags || [];

            const html = `
                ${this._refreshIndicatorHtml()}
                <a href="#/thr/submissions" class="back-link">&larr; Back to Threads Posts</a>
                <div class="detail-header">
                    ${sub.thumbnail_url ? `<img class="detail-thumb" src="${Utils.escapeHtml(sub.thumbnail_url)}" alt="" style="max-width:160px;border-radius:8px;margin-right:16px">` : ''}
                    <div class="detail-info">
                        <h2>${Utils.escapeHtml(sub.title)}</h2>
                        <div class="detail-meta">by ${Utils.escapeHtml(sub.username)} &middot; ${Utils.formatDate(sub.posted_at)} &middot; ${Utils.escapeHtml(Components.THR_TYPE_LABELS[sub.content_type] || sub.content_type || 'Text')}</div>
                        <div class="detail-meta"><a href="${Utils.escapeHtml(Utils.safeUrl(sub.link) || '#')}" target="_blank">View on Threads</a></div>
                        <div class="detail-stats">
                            <div class="detail-stat">${Utils.formatNumber(sub.views || 0)} <span class="lbl">views</span></div>
                            <div class="detail-stat">${Utils.formatNumber(sub.likes || 0)} <span class="lbl">likes</span></div>
                            <div class="detail-stat">${Utils.formatNumber(sub.reposts || 0)} <span class="lbl">reposts</span></div>
                            <div class="detail-stat">${Utils.formatNumber(sub.replies || 0)} <span class="lbl">replies</span></div>
                            <div class="detail-stat">${Utils.formatNumber(sub.quotes || 0)} <span class="lbl">quotes</span></div>
                        </div>
                        <div style="margin-top:8px;display:flex;gap:8px;align-items:center;flex-wrap:wrap">
                            <button class="btn ${isPinned ? 'btn-danger' : 'btn-secondary'} btn-pin" data-platform="thr" data-id="${Utils.escapeHtml(fullId)}" style="padding:4px 10px;font-size:12px">${isPinned ? 'Unpin' : 'Pin'}</button>
                            ${currentTags.map(t => Components.tagBadge(t)).join('')}
                            <button class="btn btn-secondary btn-add-tag" data-platform="thr" data-id="${Utils.escapeHtml(fullId)}" style="padding:4px 10px;font-size:12px">+ Tag</button>
                        </div>
                        <div style="margin-top:8px">${Components.keywords(sub.keywords)}</div>
                    </div>
                </div>

                ${Components.growthRateCards(data.growth_rates, { views: 'views/day', faves: 'likes/day', comments: 'replies/day' })}

                ${Components.dateRangeBar(this._dateRange)}

                <div class="chart-container">
                    <h3>Stats Over Time</h3>
                    <div class="chart-wrap"><canvas id="chart-detail"></canvas></div>
                </div>
            `;

            this._setContent(html);

            if (data.snapshots && data.snapshots.length > 0) {
                Charts.submissionLine('chart-detail', data.snapshots, ['views', 'likes', 'reposts', 'replies']);
            }

            this._bindDateRange(async () => {
                const range = Utils.getDateRange(this._dateRange);
                const snaps = await API.getTHRSnapshots(postId, range);
                Charts.submissionLine('chart-detail', snaps.snapshots, ['views', 'likes', 'reposts', 'replies']);
            });

            this._bindDetailPinTag('thr', fullId, allTags.tags || [], () => this.renderTHRDetail(postId));
            this._startAutoRefresh(() => this.renderTHRDetail(postId));
        } catch (err) {
            this._setContent(`<div class="empty-state"><h3>Error loading THR post</h3><p>${Utils.escapeHtml(err.message)}</p></div>`);
        }
    },

    // ── THR Compare ─────────────────────────────────────────────

    async renderTHRCompare() {
        this._loading();
        try {
            const data = await API.getTHRSubmissions({ sort_by: 'views', order: 'desc', account_id: this._acctId('thr') });
            const subs = data.submissions;

            const chips = subs.map(s => `
                <label class="compare-chip ${this._thrCompareIds.has(String(s.submission_id)) ? 'selected' : ''}" data-id="${Utils.escapeHtml(String(s.submission_id))}">
                    <input type="checkbox" ${this._thrCompareIds.has(String(s.submission_id)) ? 'checked' : ''}>
                    ${Utils.escapeHtml(Utils.truncate(s.title, 25))}
                </label>
            `).join('');

            const html = `
                ${this._refreshIndicatorHtml()}
                <div class="page-header">
                    <h2>Compare Threads Posts</h2>
                    <div>
                        <select class="filter-select" id="compare-metric">
                            <option value="views" ${this._thrCompareMetric === 'views' ? 'selected' : ''}>Views</option>
                            <option value="likes" ${this._thrCompareMetric === 'likes' ? 'selected' : ''}>Likes</option>
                            <option value="reposts" ${this._thrCompareMetric === 'reposts' ? 'selected' : ''}>Reposts</option>
                            <option value="replies" ${this._thrCompareMetric === 'replies' ? 'selected' : ''}>Replies</option>
                        </select>
                    </div>
                </div>
                <p style="font-size:13px;color:var(--text-muted);margin-bottom:12px">Select 2-5 Threads posts to compare their trends over time.</p>
                <div class="compare-select">${chips}</div>

                ${Components.dateRangeBar(this._dateRange)}

                <div class="chart-container" id="compare-chart-container" style="${this._thrCompareIds.size < 2 ? 'display:none' : ''}">
                    <h3>Comparison</h3>
                    <div class="chart-wrap"><canvas id="chart-compare"></canvas></div>
                </div>
                ${this._thrCompareIds.size < 2 ? '<div class="empty-state"><p>Select at least 2 posts above to see their trends compared.</p></div>' : ''}
            `;

            this._setContent(html);

            document.querySelectorAll('.compare-chip').forEach(chip => {
                chip.addEventListener('click', (e) => {
                    e.preventDefault();
                    const id = chip.dataset.id;
                    if (this._thrCompareIds.has(id)) {
                        this._thrCompareIds.delete(id);
                    } else if (this._thrCompareIds.size < 5) {
                        this._thrCompareIds.add(id);
                    }
                    this.renderTHRCompare();
                });
            });

            const metricSelect = document.getElementById('compare-metric');
            if (metricSelect) {
                metricSelect.addEventListener('change', () => {
                    this._thrCompareMetric = metricSelect.value;
                    this._loadTHRComparisonChart();
                });
            }

            this._bindDateRange(() => this._loadTHRComparisonChart());

            if (this._thrCompareIds.size >= 2) {
                await this._loadTHRComparisonChart();
            }

            this._startAutoRefresh(() => this.renderTHRCompare());
        } catch (err) {
            this._setContent(`<div class="empty-state"><h3>Error</h3><p>${Utils.escapeHtml(err.message)}</p></div>`);
        }
    },

    async _loadTHRComparisonChart() {
        try {
            if (this._thrCompareIds.size < 2) return;
            const range = Utils.getDateRange(this._dateRange);
            const data = await API.getTHRComparison([...this._thrCompareIds], range);
            const container = document.getElementById('compare-chart-container');
            if (container) container.style.display = '';
            Charts.comparisonLine('chart-compare', data.series, data.titles, this._thrCompareMetric);
        } catch (e) {
            console.error('Failed to load THR comparison chart:', e);
        }
    },

    // ── IG Dashboard ─────────────────────────────────────────
    // Instagram dashboard with Views, Likes, Reach, Comments (+ Shares).

    async renderIGDashboard() {
        this._loading();
        try {
            const [summary, agg, pins, goals] = await Promise.all([
                API.getIGSummary({ account_id: this._acctId('ig') }),
                API.getIGAggregate({ ...Utils.getDateRange(this._dateRange), account_id: this._acctId('ig') }),
                API.getPins().catch(() => ({ pins: [] })),
                API.getGoals().catch(() => ({ goals: [] })),
            ]);
            const igPins = (pins.pins || []).filter(p => p.platform === 'ig');
            const igGoals = (goals.goals || []).filter(g => g.platform === 'ig' || g.platform === 'all');

            const igHealth = window.PlatformHealth && window.PlatformHealth.get('ig');
            const isUnconfigured = igHealth && igHealth.configured === false;
            if (isUnconfigured || (summary.total_submissions || 0) === 0) {
                this._setContent(`
                    ${this._refreshIndicatorHtml()}
                    <div class="page-header"><h2>Instagram Dashboard</h2></div>
                    ${Components.platformEmptyState('ig', isUnconfigured ? {} : { reason: 'Instagram is configured but no posts have been polled yet. The first poll may still be running.' })}
                `);
                return;
            }

            const html = `
                ${this._refreshIndicatorHtml()}
                <div class="page-header">
                    <h2>Instagram Dashboard</h2>
                    <div style="display:flex;gap:8px">
                        <button class="btn btn-primary" data-poll="ig">Poll Now</button>
                        <button class="btn btn-secondary" data-resync="ig">Full Resync</button>
                        <button class="btn btn-secondary" data-export="ig">Export CSV</button>
                    </div>
                </div>

                ${igPins.length ? Components.pinnedSubmissions(igPins, 'ig') : ''}
                ${igGoals.length ? `<div class="goals-section"><h3>Goals</h3>${Components.goalProgressCards(igGoals)}</div>` : ''}

                <div class="stats-grid">
                    ${Components.statCard('Total Posts', summary.total_submissions, null, '#/ig/submissions')}
                    ${Components.statCard('Total Views', summary.total_views || 0)}
                    ${Components.statCard('Total Likes', summary.total_likes || 0)}
                    ${Components.statCard('Total Reach', summary.total_reach || 0)}
                    ${Components.statCard('Total Comments', summary.total_comments || 0)}
                </div>

                ${summary.growth_rates ? Components.growthRateCards(summary.growth_rates, { views: 'views/day', faves: 'likes/day', comments: 'comments/day' }) : ''}

                ${Components.dateRangeBar(this._dateRange)}

                <div class="chart-container">
                    <h3>Views Over Time (Aggregate)</h3>
                    <div class="chart-wrap"><canvas id="chart-agg-views"></canvas></div>
                </div>

                <div class="chart-row">
                    <div class="chart-container">
                        <h3>Top Viewed</h3>
                        ${Components.igTopList(summary.top_viewed, 'views', 'title', 'submission_id')}
                    </div>
                    <div class="chart-container">
                        <h3>Top Liked</h3>
                        ${Components.igTopList(summary.top_liked, 'likes', 'title', 'submission_id')}
                    </div>
                </div>

                <div class="chart-row">
                    <div class="chart-container">
                        <h3>Fastest Growing (24h)</h3>
                        ${Components.igTopList(summary.fastest_growing, 'views_gained', 'title', 'submission_id')}
                    </div>
                </div>
            `;

            this._setContent(html);

            if (agg.snapshots && agg.snapshots.length > 0) {
                Charts.aggregateLine('chart-agg-views', agg.snapshots, ['views']);
            }

            this._bindDateRange(() => this.renderIGDashboard());
            this._bindPinAndGoalActions(() => this.renderIGDashboard());
            this._startAutoRefresh(() => this.renderIGDashboard());
        } catch (err) {
            this._setContent(`<div class="empty-state"><h3>Error loading IG dashboard</h3><p>${Utils.escapeHtml(err.message)}</p></div>`);
        }
    },

    // ── IG Submissions ─────────────────────────────────────────

    async renderIGSubmissions() {
        this._loading();
        try {
            const data = await API.getIGSubmissions({
                sort_by: this._igSortState.field,
                order: this._igSortState.order,
                account_id: this._acctId('ig'),
            });

            const _vm = localStorage.getItem('pp-view-mode') || 'grid';
            const igGridRenderer = (subs) => Components.submissionCardGrid(
                subs,
                {
                    idKey: 'submission_id', titleKey: 'title', thumbKey: 'thumbnail_url', proxyThumb: false,
                    typeKey: 'content_type', typeLabels: Components.IG_TYPE_LABELS,
                    detailRoute: '/ig/submission', dateKey: 'posted_at',
                    stats: [
                        { key: 'views', deltaKey: 'views_delta', label: 'views' },
                        { key: 'likes', deltaKey: 'likes_delta', label: 'likes' },
                        { key: 'comments', deltaKey: 'comments_delta', label: 'comments' },
                    ],
                }
            );
            const gridHtml = igGridRenderer(data.submissions);
            const html = `
                ${this._refreshIndicatorHtml()}
                <div class="page-header"><h2>Instagram Posts</h2></div>
                <div class="toolbar">
                    <input type="text" class="search-input" id="search-input" placeholder="Search posts...">
                    <div class="view-toggle">
                        <button class="view-toggle-btn ${_vm === 'grid' ? 'active' : ''}" data-view="grid" title="Grid view">&#9638;</button>
                        <button class="view-toggle-btn ${_vm === 'list' ? 'active' : ''}" data-view="list" title="List view">&#9776;</button>
                    </div>
                </div>
                <div id="grid-container" style="${_vm !== 'grid' ? 'display:none' : ''}">${gridHtml}</div>
                <div id="table-container" class="table-scroll" style="${_vm !== 'list' ? 'display:none' : ''}">
                    ${Components.igSubmissionsTable(data.submissions)}
                </div>
            `;

            this._setContent(html);
            this._bindViewToggle();
            this._bindIGTableSort();
            this._bindIGSearch(data.submissions, igGridRenderer);
            this._startAutoRefresh(() => this.renderIGSubmissions());
        } catch (err) {
            this._setContent(`<div class="empty-state"><h3>Error loading IG submissions</h3><p>${Utils.escapeHtml(err.message)}</p></div>`);
        }
    },

    // ── IG Submission Detail ───────────────────────────────────

    async renderIGDetail(postId) {
        this._loading();
        try {
            const [data, pins, allTags] = await Promise.all([
                API.getIGSubmission(postId),
                API.getPins().catch(() => ({ pins: [] })),
                API.getTags().catch(() => ({ tags: [] })),
            ]);
            const sub = data.submission;
            const fullId = sub.submission_id;
            const isPinned = (pins.pins || []).some(p => p.platform === 'ig' && String(p.submission_id) === String(fullId));
            const currentTags = sub.tags || [];

            const html = `
                ${this._refreshIndicatorHtml()}
                <a href="#/ig/submissions" class="back-link">&larr; Back to Instagram Posts</a>
                <div class="detail-header">
                    ${sub.thumbnail_url ? `<img class="detail-thumb" src="${Utils.escapeHtml(sub.thumbnail_url)}" alt="" style="max-width:160px;border-radius:8px;margin-right:16px">` : ''}
                    <div class="detail-info">
                        <h2>${Utils.escapeHtml(sub.title)}</h2>
                        <div class="detail-meta">by ${Utils.escapeHtml(sub.username)} &middot; ${Utils.formatDate(sub.posted_at)} &middot; ${Utils.escapeHtml(Components.IG_TYPE_LABELS[sub.content_type] || sub.content_type || 'Text')}</div>
                        <div class="detail-meta"><a href="${Utils.escapeHtml(Utils.safeUrl(sub.link) || '#')}" target="_blank">View on Instagram</a></div>
                        <div class="detail-stats">
                            <div class="detail-stat">${Utils.formatNumber(sub.views || 0)} <span class="lbl">views</span></div>
                            <div class="detail-stat">${Utils.formatNumber(sub.likes || 0)} <span class="lbl">likes</span></div>
                            <div class="detail-stat">${Utils.formatNumber(sub.reach || 0)} <span class="lbl">reach</span></div>
                            <div class="detail-stat">${Utils.formatNumber(sub.comments || 0)} <span class="lbl">comments</span></div>
                            <div class="detail-stat">${Utils.formatNumber(sub.shares || 0)} <span class="lbl">shares</span></div>
                        </div>
                        <div style="margin-top:8px;display:flex;gap:8px;align-items:center;flex-wrap:wrap">
                            <button class="btn ${isPinned ? 'btn-danger' : 'btn-secondary'} btn-pin" data-platform="ig" data-id="${Utils.escapeHtml(fullId)}" style="padding:4px 10px;font-size:12px">${isPinned ? 'Unpin' : 'Pin'}</button>
                            ${currentTags.map(t => Components.tagBadge(t)).join('')}
                            <button class="btn btn-secondary btn-add-tag" data-platform="ig" data-id="${Utils.escapeHtml(fullId)}" style="padding:4px 10px;font-size:12px">+ Tag</button>
                        </div>
                        <div style="margin-top:8px">${Components.keywords(sub.keywords)}</div>
                    </div>
                </div>

                ${Components.growthRateCards(data.growth_rates, { views: 'views/day', faves: 'likes/day', comments: 'comments/day' })}

                ${Components.dateRangeBar(this._dateRange)}

                <div class="chart-container">
                    <h3>Stats Over Time</h3>
                    <div class="chart-wrap"><canvas id="chart-detail"></canvas></div>
                </div>
            `;

            this._setContent(html);

            if (data.snapshots && data.snapshots.length > 0) {
                Charts.submissionLine('chart-detail', data.snapshots, ['views', 'likes', 'reach', 'comments']);
            }

            this._bindDateRange(async () => {
                const range = Utils.getDateRange(this._dateRange);
                const snaps = await API.getIGSnapshots(postId, range);
                Charts.submissionLine('chart-detail', snaps.snapshots, ['views', 'likes', 'reach', 'comments']);
            });

            this._bindDetailPinTag('ig', fullId, allTags.tags || [], () => this.renderIGDetail(postId));
            this._startAutoRefresh(() => this.renderIGDetail(postId));
        } catch (err) {
            this._setContent(`<div class="empty-state"><h3>Error loading IG post</h3><p>${Utils.escapeHtml(err.message)}</p></div>`);
        }
    },

    // ── IG Compare ─────────────────────────────────────────────

    async renderIGCompare() {
        this._loading();
        try {
            const data = await API.getIGSubmissions({ sort_by: 'views', order: 'desc', account_id: this._acctId('ig') });
            const subs = data.submissions;

            const chips = subs.map(s => `
                <label class="compare-chip ${this._igCompareIds.has(String(s.submission_id)) ? 'selected' : ''}" data-id="${Utils.escapeHtml(String(s.submission_id))}">
                    <input type="checkbox" ${this._igCompareIds.has(String(s.submission_id)) ? 'checked' : ''}>
                    ${Utils.escapeHtml(Utils.truncate(s.title, 25))}
                </label>
            `).join('');

            const html = `
                ${this._refreshIndicatorHtml()}
                <div class="page-header">
                    <h2>Compare Instagram Posts</h2>
                    <div>
                        <select class="filter-select" id="compare-metric">
                            <option value="views" ${this._igCompareMetric === 'views' ? 'selected' : ''}>Views</option>
                            <option value="likes" ${this._igCompareMetric === 'likes' ? 'selected' : ''}>Likes</option>
                            <option value="reach" ${this._igCompareMetric === 'reach' ? 'selected' : ''}>Reach</option>
                            <option value="comments" ${this._igCompareMetric === 'comments' ? 'selected' : ''}>Comments</option>
                        </select>
                    </div>
                </div>
                <p style="font-size:13px;color:var(--text-muted);margin-bottom:12px">Select 2-5 Instagram posts to compare their trends over time.</p>
                <div class="compare-select">${chips}</div>

                ${Components.dateRangeBar(this._dateRange)}

                <div class="chart-container" id="compare-chart-container" style="${this._igCompareIds.size < 2 ? 'display:none' : ''}">
                    <h3>Comparison</h3>
                    <div class="chart-wrap"><canvas id="chart-compare"></canvas></div>
                </div>
                ${this._igCompareIds.size < 2 ? '<div class="empty-state"><p>Select at least 2 posts above to see their trends compared.</p></div>' : ''}
            `;

            this._setContent(html);

            document.querySelectorAll('.compare-chip').forEach(chip => {
                chip.addEventListener('click', (e) => {
                    e.preventDefault();
                    const id = chip.dataset.id;
                    if (this._igCompareIds.has(id)) {
                        this._igCompareIds.delete(id);
                    } else if (this._igCompareIds.size < 5) {
                        this._igCompareIds.add(id);
                    }
                    this.renderIGCompare();
                });
            });

            const metricSelect = document.getElementById('compare-metric');
            if (metricSelect) {
                metricSelect.addEventListener('change', () => {
                    this._igCompareMetric = metricSelect.value;
                    this._loadIGComparisonChart();
                });
            }

            this._bindDateRange(() => this._loadIGComparisonChart());

            if (this._igCompareIds.size >= 2) {
                await this._loadIGComparisonChart();
            }

            this._startAutoRefresh(() => this.renderIGCompare());
        } catch (err) {
            this._setContent(`<div class="empty-state"><h3>Error</h3><p>${Utils.escapeHtml(err.message)}</p></div>`);
        }
    },

    async _loadIGComparisonChart() {
        try {
            if (this._igCompareIds.size < 2) return;
            const range = Utils.getDateRange(this._dateRange);
            const data = await API.getIGComparison([...this._igCompareIds], range);
            const container = document.getElementById('compare-chart-container');
            if (container) container.style.display = '';
            Charts.comparisonLine('chart-compare', data.series, data.titles, this._igCompareMetric);
        } catch (e) {
            console.error('Failed to load IG comparison chart:', e);
        }
    },

    // ── TW Dashboard ─────────────────────────────────────────
    // X/Twitter dashboard with Views, Likes, Retweets, Replies, Quotes, Bookmarks.

    async renderTWDashboard() {
        this._loading();
        try {
            const [summary, agg, pins, goals] = await Promise.all([
                API.getTWSummary({ account_id: this._acctId('tw') }),
                API.getTWAggregate({ ...Utils.getDateRange(this._dateRange), account_id: this._acctId('tw') }),
                API.getPins().catch(() => ({ pins: [] })),
                API.getGoals().catch(() => ({ goals: [] })),
            ]);
            const twPins = (pins.pins || []).filter(p => p.platform === 'tw');
            const twGoals = (goals.goals || []).filter(g => g.platform === 'tw' || g.platform === 'all');

            // Empty-state short-circuit: see SF dashboard for the pattern.
            const twHealth = window.PlatformHealth && window.PlatformHealth.get('tw');
            const isUnconfigured = twHealth && twHealth.configured === false;
            if (isUnconfigured || (summary.total_submissions || 0) === 0) {
                this._setContent(`
                    ${this._refreshIndicatorHtml()}
                    <div class="page-header"><h2>X/Twitter Dashboard</h2></div>
                    ${Components.platformEmptyState('tw', isUnconfigured ? {} : { reason: 'X/Twitter is configured but no tweets have been polled yet. The first poll may still be running.' })}
                `);
                return;
            }

            const html = `
                ${this._refreshIndicatorHtml()}
                <div class="page-header">
                    <h2>X/Twitter Dashboard</h2>
                    <div style="display:flex;gap:8px">
                        <button class="btn btn-primary" data-poll="tw">Poll Now</button>
                        <button class="btn btn-secondary" data-resync="tw">Full Resync</button>
                        <button class="btn btn-secondary" data-export="tw">Export CSV</button>
                    </div>
                </div>

                ${twPins.length ? Components.pinnedSubmissions(twPins, 'tw') : ''}
                ${twGoals.length ? `<div class="goals-section"><h3>Goals</h3>${Components.goalProgressCards(twGoals)}</div>` : ''}

                <div class="stats-grid">
                    ${Components.statCard('Total Tweets', summary.total_submissions, null, '#/tw/submissions')}
                    ${Components.statCard('Total Views', summary.total_views || 0)}
                    ${Components.statCard('Total Likes', summary.total_likes || 0)}
                    ${Components.statCard('Total Retweets', summary.total_retweets || 0)}
                    ${Components.statCard('Total Replies', summary.total_comments || summary.total_replies || 0)}
                    ${Components.statCard('Total Quotes', summary.total_quotes || 0)}
                    ${Components.statCard('Total Bookmarks', summary.total_bookmarks || 0)}
                </div>

                ${summary.growth_rates ? (() => {
                    const rates = {};
                    for (const period of ['24h', '7d', '30d']) {
                        const r = summary.growth_rates[period];
                        if (r) {
                            rates[period] = {
                                views_per_day: r.views_per_day != null ? r.views_per_day : 0,
                                faves_per_day: r.likes_per_day != null ? r.likes_per_day : 0,
                                comments_per_day: r.comments_per_day || r.replies_per_day || 0,
                            };
                        }
                    }
                    return Components.growthRateCards(rates, { views: 'views/day', faves: 'likes/day', comments: 'replies/day' });
                })() : ''}

                ${Components.dateRangeBar(this._dateRange)}

                <div class="chart-container">
                    <h3>Views Over Time (Aggregate)</h3>
                    <div class="chart-wrap"><canvas id="chart-agg-views"></canvas></div>
                </div>

                <div class="chart-row">
                    <div class="chart-container">
                        <h3>Top Viewed</h3>
                        ${Components.twTopList(summary.top_viewed, 'views', 'title', 'submission_id')}
                    </div>
                    <div class="chart-container">
                        <h3>Top Liked</h3>
                        ${Components.twTopList(summary.top_liked || summary.top_faved, 'likes', 'title', 'submission_id')}
                    </div>
                </div>

                <div class="chart-row">
                    <div class="chart-container">
                        <h3>Top Retweeted</h3>
                        ${Components.twTopList(summary.top_retweeted, 'retweets', 'title', 'submission_id')}
                    </div>
                    <div class="chart-container">
                        <h3>Fastest Growing (24h)</h3>
                        ${Components.twTopList(summary.fastest_growing, 'views_gained', 'title', 'submission_id')}
                    </div>
                </div>
            `;

            this._setContent(html);

            if (agg.snapshots && agg.snapshots.length > 0) {
                Charts.aggregateLine('chart-agg-views', agg.snapshots, ['views']);
            }

            this._loadFollowerWidget('tw', this._acctId('tw'));
            this._bindDateRange(() => this.renderTWDashboard());
            this._bindPinAndGoalActions(() => this.renderTWDashboard());
            this._startAutoRefresh(() => this.renderTWDashboard());
        } catch (err) {
            this._setContent(`<div class="empty-state"><h3>Error loading TW dashboard</h3><p>${Utils.escapeHtml(err.message)}</p></div>`);
        }
    },

    // ── TW Submissions ─────────────────────────────────────────

    async renderTWSubmissions() {
        this._loading();
        try {
            const data = await API.getTWSubmissions({
                sort_by: this._twSortState.field,
                order: this._twSortState.order,
                account_id: this._acctId('tw'),
            });

            const _vm = localStorage.getItem('pp-view-mode') || 'grid';
            // 2.16.14 (BUG-021): closure so the search filter can re-render
            const twGridRenderer = (subs) => Components.submissionCardGrid(subs, {
                idKey: 'submission_id', titleKey: 'title', thumbKey: 'thumbnail_url', proxyThumb: false,
                typeKey: 'content_type', typeLabels: Components.TW_TYPE_LABELS,
                detailRoute: '/tw/submission', dateKey: 'posted_at',
                stats: [
                    { key: 'views', deltaKey: 'views_delta', label: 'views' },
                    { key: 'likes', deltaKey: 'likes_delta', label: 'likes' },
                    { key: 'retweets', deltaKey: 'retweets_delta', label: 'retweets' },
                ],
            });
            const gridHtml = twGridRenderer(data.submissions);
            const html = `
                ${this._refreshIndicatorHtml()}
                <div class="page-header"><h2>X/Twitter Tweets</h2></div>
                <div class="toolbar">
                    <input type="text" class="search-input" id="search-input" placeholder="Search tweets...">
                    <div class="view-toggle">
                        <button class="view-toggle-btn ${_vm === 'grid' ? 'active' : ''}" data-view="grid" title="Grid view">&#9638;</button>
                        <button class="view-toggle-btn ${_vm === 'list' ? 'active' : ''}" data-view="list" title="List view">&#9776;</button>
                    </div>
                </div>
                <div id="grid-container" style="${_vm !== 'grid' ? 'display:none' : ''}">${gridHtml}</div>
                <div id="table-container" class="table-scroll" style="${_vm !== 'list' ? 'display:none' : ''}">
                    ${Components.twSubmissionsTable(data.submissions)}
                </div>
            `;

            this._setContent(html);
            this._bindViewToggle();
            this._bindTWTableSort();
            this._bindTWSearch(data.submissions, twGridRenderer);
            this._startAutoRefresh(() => this.renderTWSubmissions());
        } catch (err) {
            this._setContent(`<div class="empty-state"><h3>Error loading TW submissions</h3><p>${Utils.escapeHtml(err.message)}</p></div>`);
        }
    },

    // ── TW Submission Detail ───────────────────────────────────

    async renderTWDetail(id) {
        this._loading();
        try {
            const [data, pins, allTags] = await Promise.all([
                API.getTWSubmission(id),
                API.getPins().catch(() => ({ pins: [] })),
                API.getTags().catch(() => ({ tags: [] })),
            ]);
            const sub = data.submission;
            const isPinned = (pins.pins || []).some(p => p.platform === 'tw' && String(p.submission_id) === String(id));
            const currentTags = sub.tags || [];

            const html = `
                ${this._refreshIndicatorHtml()}
                <a href="#/tw/submissions" class="back-link">&larr; Back to X/Twitter Tweets</a>
                <div class="detail-header">
                    <div class="detail-info">
                        <h2>${Utils.escapeHtml(sub.title)}</h2>
                        <div class="detail-meta">by ${Utils.escapeHtml(sub.username)} &middot; ${Utils.formatDate(sub.posted_at)} &middot; ${Utils.escapeHtml(Components.TW_TYPE_LABELS[sub.content_type] || sub.content_type || 'Tweet')}</div>
                        <div class="detail-meta"><a href="${Utils.escapeHtml(Utils.safeUrl(sub.link) || '#')}" target="_blank">View on X</a></div>
                        ${sub.thumbnail_url ? `<div class="detail-media"><a href="${Utils.escapeHtml(Utils.safeUrl(sub.link) || '#')}" target="_blank"><img src="${Utils.escapeHtml(sub.thumbnail_url)}" loading="lazy" alt="tweet image" style="max-width:340px;max-height:340px;border-radius:var(--radius,12px);margin:10px 0;border:1px solid var(--border)"></a></div>` : ''}
                        <div class="detail-stats">
                            <div class="detail-stat">${Utils.formatNumber(sub.views || 0)} <span class="lbl">views</span></div>
                            <div class="detail-stat">${Utils.formatNumber(sub.likes || 0)} <span class="lbl">likes</span></div>
                            <div class="detail-stat">${Utils.formatNumber(sub.retweets || 0)} <span class="lbl">retweets</span></div>
                            <div class="detail-stat">${Utils.formatNumber(sub.replies || 0)} <span class="lbl">replies</span></div>
                            <div class="detail-stat">${Utils.formatNumber(sub.quotes || 0)} <span class="lbl">quotes</span></div>
                            <div class="detail-stat">${Utils.formatNumber(sub.bookmarks || 0)} <span class="lbl">bookmarks</span></div>
                        </div>
                        <div style="margin-top:8px;display:flex;gap:8px;align-items:center;flex-wrap:wrap">
                            <button class="btn ${isPinned ? 'btn-danger' : 'btn-secondary'} btn-pin" data-platform="tw" data-id="${id}" style="padding:4px 10px;font-size:12px">${isPinned ? 'Unpin' : 'Pin'}</button>
                            ${currentTags.map(t => Components.tagBadge(t)).join('')}
                            <button class="btn btn-secondary btn-add-tag" data-platform="tw" data-id="${id}" style="padding:4px 10px;font-size:12px">+ Tag</button>
                        </div>
                        <div style="margin-top:8px">${Components.keywords(sub.keywords)}</div>
                    </div>
                </div>

                ${Components.growthRateCards(data.growth_rates)}

                ${Components.dateRangeBar(this._dateRange)}

                <div class="chart-container">
                    <h3>Stats Over Time</h3>
                    <div class="chart-wrap"><canvas id="chart-detail"></canvas></div>
                </div>
            `;

            this._setContent(html);

            if (data.snapshots && data.snapshots.length > 0) {
                Charts.submissionLine('chart-detail', data.snapshots, ['views', 'likes', 'retweets', 'replies']);
            }

            this._bindDateRange(async () => {
                const range = Utils.getDateRange(this._dateRange);
                const snaps = await API.getTWSnapshots(id, range);
                Charts.submissionLine('chart-detail', snaps.snapshots, ['views', 'likes', 'retweets', 'replies']);
            });

            this._bindDetailPinTag('tw', id, allTags.tags || [], () => this.renderTWDetail(id));
            this._startAutoRefresh(() => this.renderTWDetail(id));
        } catch (err) {
            this._setContent(`<div class="empty-state"><h3>Error loading tweet</h3><p>${Utils.escapeHtml(err.message)}</p></div>`);
        }
    },

    // ── TW Compare ─────────────────────────────────────────────

    async renderTWCompare() {
        this._loading();
        try {
            const data = await API.getTWSubmissions({ sort_by: 'views', order: 'desc', account_id: this._acctId('tw') });
            const subs = data.submissions;

            const chips = subs.map(s => `
                <label class="compare-chip ${this._twCompareIds.has(s.submission_id) ? 'selected' : ''}" data-id="${s.submission_id}">
                    <input type="checkbox" ${this._twCompareIds.has(s.submission_id) ? 'checked' : ''}>
                    ${Utils.escapeHtml(Utils.truncate(s.title, 25))}
                </label>
            `).join('');

            const html = `
                ${this._refreshIndicatorHtml()}
                <div class="page-header">
                    <h2>Compare X/Twitter Tweets</h2>
                    <div>
                        <select class="filter-select" id="compare-metric">
                            <option value="views" ${this._twCompareMetric === 'views' ? 'selected' : ''}>Views</option>
                            <option value="likes" ${this._twCompareMetric === 'likes' ? 'selected' : ''}>Likes</option>
                            <option value="retweets" ${this._twCompareMetric === 'retweets' ? 'selected' : ''}>Retweets</option>
                            <option value="replies" ${this._twCompareMetric === 'replies' ? 'selected' : ''}>Replies</option>
                            <option value="quotes" ${this._twCompareMetric === 'quotes' ? 'selected' : ''}>Quotes</option>
                            <option value="bookmarks" ${this._twCompareMetric === 'bookmarks' ? 'selected' : ''}>Bookmarks</option>
                        </select>
                    </div>
                </div>
                <p style="font-size:13px;color:var(--text-muted);margin-bottom:12px">Select 2-5 tweets to compare their trends over time.</p>
                <div class="compare-select">${chips}</div>

                ${Components.dateRangeBar(this._dateRange)}

                <div class="chart-container" id="compare-chart-container" style="${this._twCompareIds.size < 2 ? 'display:none' : ''}">
                    <h3>Comparison</h3>
                    <div class="chart-wrap"><canvas id="chart-compare"></canvas></div>
                </div>
                ${this._twCompareIds.size < 2 ? '<div class="empty-state"><p>Select at least 2 tweets above to see their trends compared.</p></div>' : ''}
            `;

            this._setContent(html);

            document.querySelectorAll('.compare-chip').forEach(chip => {
                chip.addEventListener('click', (e) => {
                    e.preventDefault();
                    const id = chip.dataset.id;
                    if (this._twCompareIds.has(id)) {
                        this._twCompareIds.delete(id);
                    } else if (this._twCompareIds.size < 5) {
                        this._twCompareIds.add(id);
                    }
                    this.renderTWCompare();
                });
            });

            const metricSelect = document.getElementById('compare-metric');
            if (metricSelect) {
                metricSelect.addEventListener('change', () => {
                    this._twCompareMetric = metricSelect.value;
                    this._loadTWComparisonChart();
                });
            }

            this._bindDateRange(() => this._loadTWComparisonChart());

            if (this._twCompareIds.size >= 2) {
                await this._loadTWComparisonChart();
            }

            this._startAutoRefresh(() => this.renderTWCompare());
        } catch (err) {
            this._setContent(`<div class="empty-state"><h3>Error</h3><p>${Utils.escapeHtml(err.message)}</p></div>`);
        }
    },

    async _loadTWComparisonChart() {
        try {
            if (this._twCompareIds.size < 2) return;
            const range = Utils.getDateRange(this._dateRange);
            const data = await API.getTWComparison([...this._twCompareIds], range);
            const container = document.getElementById('compare-chart-container');
            if (container) container.style.display = '';
            Charts.comparisonLine('chart-compare', data.series, data.titles, this._twCompareMetric);
        } catch (e) {
            console.error('Failed to load TW comparison chart:', e);
        }
    },

    // ── Groups ────────────────────────────────────────────────
    // Submission groups management page. Groups are cross-platform collections
    // that let users organise related submissions from any platform (IB, FA, WS, SF)
    // into named groups for combined tracking. New groups are created via a
    // browser prompt() dialog asking for name and optional description.

    async renderGroups() {
        this._loading();
        try {
            const data = await API.getGroups();
            const groups = data.groups || [];

            const html = `
                <div class="page-header">
                    <h2>Submission Groups</h2>
                    <button class="btn btn-primary" id="create-group-btn">Create Group</button>
                </div>
                <p style="font-size:13px;color:var(--text-muted);margin-bottom:16px">Organise submissions from any platform into groups for combined tracking.</p>
                <div class="stats-grid">
                    ${Components.groupsList(groups)}
                </div>
            `;

            this._setContent(html);

            document.getElementById('create-group-btn').addEventListener('click', async () => {
                const name = prompt('Group name:');
                if (!name) return;
                const description = prompt('Description (optional):') || '';
                try {
                    await API.createGroup({ name, description });
                    this.renderGroups();
                } catch (err) {
                    alert('Failed: ' + err.message);
                }
            });
        } catch (err) {
            this._setContent(`<div class="empty-state"><h3>Error</h3><p>${Utils.escapeHtml(err.message)}</p></div>`);
        }
    },

    // Group detail page. Shows the group name, description, aggregated stats
    // (total views/faves/comments across all member submissions), and a member
    // table with platform badges (IB/FA/WS), title links to individual detail
    // pages, stat columns, and per-row remove buttons. "Add Submission" uses
    // prompt() dialogs for platform and submission ID. "Delete Group" confirms
    // then navigates back to the groups list.
    async renderGroupDetail(groupId) {
        this._loading();
        try {
            // Fetch group list (to find this group's metadata) and stats in parallel
            const [groupData, statsData] = await Promise.all([
                API.getGroups(),
                API.getGroupStats(groupId),
            ]);
            // Locate the specific group by ID from the full groups list
            const group = (groupData.groups || []).find(g => g.group_id === groupId);
            if (!group) { this._setContent('<div class="empty-state"><h3>Group not found</h3></div>'); return; }

            const stats = statsData;
            const members = stats.members || [];

            // Build table rows for each member: platform badge, linked title
            // (routing to the correct platform detail page), stats, and remove button
            const memberRows = members.map(m => {
                // Determine platform badge colour and the correct hash route prefix
                const badgeMap = { fa: '<span class="platform-badge fa">FA</span>', ws: '<span class="platform-badge ws">WS</span>', sf: '<span class="platform-badge sf">SF</span>', sqw: '<span class="platform-badge sqw">SqW</span>', ao3: '<span class="platform-badge ao3">AO3</span>', da: '<span class="platform-badge da">DA</span>', wp: '<span class="platform-badge wp">WP</span>', ik: '<span class="platform-badge ik">IK</span>', bsky: '<span class="platform-badge bsky">BSKY</span>', tw: '<span class="platform-badge tw">TW</span>', ib: '<span class="platform-badge ib">IB</span>' };
                const badge = badgeMap[m.platform] || badgeMap.ib;
                const prefixMap = { fa: '/fa/submission/', ws: '/ws/submission/', sf: '/sf/submission/', sqw: '/sqw/submission/', ao3: '/ao3/submission/', da: '/da/submission/', wp: '/wp/submission/', ik: '/ik/submission/', bsky: '/bsky/submission/', tw: '/tw/submission/', mast: '/mast/submission/', tum: '/tum/submission/', pix: '/pix/submission/', thr: '/thr/submission/', ig: '/ig/submission/', e621: '/e621/submission/', fn: '/fn/submission/', fbr: '/fbr/submission/', ib: '/submission/' };
                const prefix = prefixMap[m.platform] || prefixMap.ib;
                return `
                    <tr>
                        <td>${badge}</td>
                        <td><a href="#${prefix}${m.submission_id}">${Utils.escapeHtml(Utils.truncate(m.title || '#' + m.submission_id, 40))}</a></td>
                        <td>${Utils.formatNumber(m.views || 0)}</td>
                        <td>${Utils.formatNumber(m.favorites_count || 0)}</td>
                        <td>${Utils.formatNumber(m.comments_count || 0)}</td>
                        <td><button class="btn btn-danger" style="font-size:11px;padding:2px 8px" data-remove-group="${groupId}" data-remove-platform="${m.platform}" data-remove-sub="${m.submission_id}">Remove</button></td>
                    </tr>
                `;
            }).join('');

            const html = `
                <a href="#/groups" class="back-link">&larr; Back to Groups</a>
                <div class="page-header">
                    <h2>${Utils.escapeHtml(group.name)}</h2>
                    <div style="display:flex;gap:8px">
                        <button class="btn btn-primary" id="add-member-btn">Add Submission</button>
                        <button class="btn btn-danger" id="delete-group-btn">Delete Group</button>
                    </div>
                </div>
                ${group.description ? `<p style="color:var(--text-muted);font-size:13px;margin-bottom:16px">${Utils.escapeHtml(group.description)}</p>` : ''}

                <div class="stats-grid">
                    ${Components.statCard('Total Views', stats.total_views || 0)}
                    ${Components.statCard('Total Favorites', stats.total_favorites || 0)}
                    ${Components.statCard('Total Comments', stats.total_comments || 0)}
                    ${Components.statCard('Members', members.length)}
                </div>

                <table class="data-table">
                    <thead><tr><th style="width:40px"></th><th>Title</th><th>Views</th><th>Faves</th><th>Comments</th><th style="width:80px"></th></tr></thead>
                    <tbody>${memberRows || '<tr><td colspan="6" style="text-align:center;color:var(--text-muted)">No members yet</td></tr>'}</tbody>
                </table>
            `;

            this._setContent(html);

            // "Add Submission" — pick a discovered submission via the visual
            // WorkPicker (2.162.0), replacing the old platform-then-ID prompt pair.
            document.getElementById('add-member-btn').addEventListener('click', () => {
                if (!window.WorkPicker) { alert('Picker unavailable'); return; }
                WorkPicker.open({
                    title: 'Add a submission to this group',
                    confirmLabel: 'Add',
                    multi: false,
                    filters: ['discovered'],
                    onConfirm: async (items) => {
                        const it = items[0];
                        if (!it) return;
                        // Discovered items carry member_ref "platform:submission_id".
                        const idx = it.member_ref.indexOf(':');
                        const platform = it.member_ref.slice(0, idx);
                        const sid = it.member_ref.slice(idx + 1);
                        await API.addGroupMember(groupId, {
                            platform,
                            submission_id: platform === 'sf' ? String(sid).trim() : parseInt(sid),
                        });
                        this.renderGroupDetail(groupId);
                    },
                });
            });

            // "Delete Group" button: confirmation dialog, then delete and navigate
            // back to the groups list page
            document.getElementById('delete-group-btn').addEventListener('click', async () => {
                if (!confirm('Delete this group? This cannot be undone.')) return;
                try {
                    await API.deleteGroup(groupId);
                    this.navigate('/groups');
                } catch (err) {
                    alert('Failed: ' + err.message);
                }
            });
        } catch (err) {
            this._setContent(`<div class="empty-state"><h3>Error</h3><p>${Utils.escapeHtml(err.message)}</p></div>`);
        }
    },

    // Confirms and removes a single submission from a group, then re-renders
    // the group detail page. Called via inline onclick on each member row's
    // remove button.
    async removeGroupMember(groupId, platform, subId) {
        if (!confirm('Remove this submission from the group?')) return;
        try {
            await API.removeGroupMember(groupId, platform, subId);
            this.renderGroupDetail(groupId);
        } catch (err) {
            alert('Failed: ' + err.message);
        }
    },

    // ── Cross-Platform ────────────────────────────────────────
    // Cross-platform links page. Links connect the same submission across
    // platforms (e.g. IB #12345 + FA #67890) so their stats can be viewed
    // together. Shows auto-suggested links (based on title matching) and
    // existing links. New links are created via a text input prompt using
    // the format "platform:id, platform:id" (e.g. "ib:12345, fa:67890").

    async renderCrossPlatform() {
        this._loading();
        try {
            // Fetch existing links and auto-suggestions in parallel;
            // suggestions endpoint failure is non-fatal (falls back to empty)
            const [linksData, suggestionsData] = await Promise.all([
                API.getLinks(),
                API.getLinkSuggestions().catch(() => ({ suggestions: [] })),
            ]);

            const html = `
                <div class="page-header">
                    <h2>Cross-Platform Links</h2>
                    <button class="btn btn-primary" id="create-link-btn">Create Link</button>
                </div>
                <p style="font-size:13px;color:var(--text-muted);margin-bottom:16px">Link the same submission across platforms to view combined analytics.</p>

                ${(suggestionsData.suggestions || []).length > 0 ? `
                <div class="chart-container">
                    <h3>Suggested Links</h3>
                    ${Components.linkSuggestions(suggestionsData.suggestions)}
                </div>` : ''}

                <div class="stats-grid">
                    ${Components.linkCards(linksData.links || [])}
                </div>

                <div id="link-stats-container"></div>
            `;

            this._setContent(html);

            // "Create Link" button: prompt for comma-separated platform:id pairs,
            // parse them into member objects, create the link via API, then re-render
            document.getElementById('create-link-btn').addEventListener('click', async () => {
                const input = prompt('Enter members as: platform:id, platform:id\nExample: ib:12345, fa:67890, ws:11111, sf:abc123');
                if (!input) return;
                try {
                    // Parse "ib:12345, fa:67890" into [{platform:"ib", submission_id:12345}, ...]
                    // SF uses alphanumeric IDs so only parseInt for non-SF platforms
                    const members = input.split(',').map(s => {
                        const [platform, id] = s.trim().split(':');
                        const p = platform.trim();
                        return { platform: p, submission_id: p === 'sf' ? id.trim() : parseInt(id.trim()) };
                    });
                    await API.createLink({ members });
                    this.renderCrossPlatform();
                } catch (err) {
                    alert('Failed: ' + err.message);
                }
            });
        } catch (err) {
            this._setContent(`<div class="empty-state"><h3>Error</h3><p>${Utils.escapeHtml(err.message)}</p></div>`);
        }
    },

    // Loads combined stats and a snapshot chart for a cross-platform link.
    // Fetches link stats and snapshots in parallel, then renders them into
    // the #link-stats-container div on the cross-platform page. Shows
    // combined views/faves/comments stat cards and an aggregate line chart.
    async viewLinkStats(linkId) {
        try {
            // Parallel fetch: aggregated stats and time-series snapshots
            const [stats, snapshots] = await Promise.all([
                API.getLinkStats(linkId),
                API.getLinkSnapshots(linkId),
            ]);
            const container = document.getElementById('link-stats-container');
            if (!container) return;

            container.innerHTML = `
                <div class="chart-container" style="margin-top:16px">
                    <h3>Combined Stats for Link #${linkId}</h3>
                    <div class="stats-grid" style="margin-bottom:12px">
                        ${Components.statCard('Combined Views', stats.total_views || 0)}
                        ${Components.statCard('Combined Faves', stats.total_favorites || 0)}
                        ${Components.statCard('Combined Comments', stats.total_comments || 0)}
                    </div>
                    ${(snapshots.snapshots || []).length > 0 ? '<div class="chart-wrap"><canvas id="chart-link-combined"></canvas></div>' : '<p style="color:var(--text-muted)">No snapshot data yet</p>'}
                </div>
            `;

            if ((snapshots.snapshots || []).length > 0) {
                Charts.aggregateLine('chart-link-combined', snapshots.snapshots, ['views', 'favorites_count', 'comments_count']);
            }
        } catch (err) {
            alert('Failed to load link stats: ' + err.message);
        }
    },

    // Confirms and deletes a cross-platform link, then re-renders the page.
    // Called from link card delete buttons.
    async deleteLink(linkId) {
        if (!confirm('Remove this cross-platform link?')) return;
        try {
            await API.deleteLink(linkId);
            this.renderCrossPlatform();
        } catch (err) {
            alert('Failed: ' + err.message);
        }
    },

    // Creates a cross-platform link from an auto-suggestion. Called when the
    // user clicks "Link" on a suggested match. The items array contains the
    // pre-built platform/submission_id member objects from the suggestion.
    async createLinkFromSuggestion(items) {
        try {
            await API.createLink({ members: items });
            this.renderCrossPlatform();
        } catch (err) {
            alert('Failed: ' + err.message);
        }
    },

    // ── Settings ──────────────────────────────────────────────
    // Massive settings page. Fetches 12 API endpoints in parallel for all state.
    // Sections: IB credentials, app preferences (tray/startup/notifications/poll
    // intervals), notification filters, auto-update, Telegram integration, FA
    // connection (cookie_a/cookie_b), WS connection (API key), poll logs for all
    // platforms. After HTML render, attaches event handlers for all interactive
    // elements: save/logout/poll/resync buttons, toggle switches, select dropdowns,
    // connect/disconnect buttons, threshold inputs, and apply-update button.

    // Danger zone — uninstall flow.
    // Two-step modal:
    //   1. GET /api/settings/uninstall/plan → show what would be deleted
    //   2. User ticks checkboxes + types "UNINSTALL" → POST /api/settings/uninstall
    // After POST the server fires a detached cleanup script and shuts itself
    // down in ~2s. We show a goodbye panel; the user closes the tab manually.
    async _showUninstallDialog() {
        let plan;
        try {
            plan = await API.get('/api/settings/uninstall/plan');
        } catch (e) {
            alert('Could not load uninstall plan: ' + e.message);
            return;
        }

        const INSTALL_TYPE_LABELS = {
            windows_installer: 'Windows installer (Inno Setup)',
            windows_portable:  'Windows portable (zip extract)',
            linux_appimage:    'Linux AppImage',
            dev:               'Dev mode (running from source)',
            unknown:           'Unknown install type',
        };
        const typeLabel = INSTALL_TYPE_LABELS[plan.install_type] || plan.install_type;
        const isDev = plan.install_type === 'dev';

        const overlay = document.createElement('div');
        overlay.className = 'modal-overlay open';  // '.modal-overlay' is display:none until '.open' is added
        overlay.style.zIndex = '10020';
        overlay.innerHTML = `
            <div class="modal" style="max-width:560px">
                <div class="modal-header" style="background:rgba(180,60,60,0.08);border-bottom:1px solid #a44">
                    <h3 style="color:#c66;margin:0">Uninstall PawPoller</h3>
                </div>
                <div class="modal-body" style="font-size:13px;line-height:1.5">
                    <p><strong>Detected install:</strong> ${Utils.escapeHtml(typeLabel)}</p>
                    <div style="background:var(--bg-secondary);padding:10px 12px;border-radius:4px;font-family:monospace;font-size:11px;margin:8px 0">
                        <div><span style="color:var(--text-muted)">app path:</span> ${Utils.escapeHtml(plan.app_path)}</div>
                        <div><span style="color:var(--text-muted)">data dir:</span> ${Utils.escapeHtml(plan.data_dir)}</div>
                        <div><span style="color:var(--text-muted)">autostart:</span> ${Utils.escapeHtml(plan.autostart_target)}</div>
                        ${plan.has_keyring_key ? '<div><span style="color:var(--text-muted)">keyring:</span> vault key present</div>' : ''}
                    </div>
                    ${isDev ? `
                        <p style="color:#c93;font-size:12px">
                            <strong>Dev mode detected.</strong> The source tree will NOT be deleted —
                            only user data and autostart entries can be cleaned up. Delete the
                            cloned folder manually if you also want the code gone.
                        </p>
                    ` : ''}
                    <p style="margin-top:14px"><strong>What to remove:</strong></p>
                    <label style="display:block;margin:6px 0">
                        <input type="checkbox" id="uninst-app" checked ${isDev ? 'disabled' : ''}>
                        Application files (${Utils.escapeHtml(plan.app_path)})
                    </label>
                    <label style="display:block;margin:6px 0">
                        <input type="checkbox" id="uninst-data" checked>
                        User data: database, settings, vault, logs (${Utils.escapeHtml(plan.data_dir)})
                    </label>
                    <label style="display:block;margin:6px 0">
                        <input type="checkbox" id="uninst-autostart" checked>
                        Autostart entry
                    </label>
                    <p style="margin-top:14px">
                        Type <code>UNINSTALL</code> to confirm:
                    </p>
                    <input type="text" id="uninst-confirm" class="search-input"
                           placeholder="UNINSTALL" autocomplete="off"
                           style="width:100%;font-family:monospace">
                    <div id="uninst-msg" style="margin-top:10px;font-size:12px"></div>
                </div>
                <div class="modal-footer">
                    <button class="btn btn-secondary" id="uninst-cancel">Cancel</button>
                    <button class="btn" id="uninst-go"
                            style="background:#a44;color:#fff;border-color:#a44" disabled>
                        Uninstall
                    </button>
                </div>
            </div>
        `;
        document.body.appendChild(overlay);

        const goBtn = overlay.querySelector('#uninst-go');
        const confirmInput = overlay.querySelector('#uninst-confirm');
        const msg = overlay.querySelector('#uninst-msg');

        confirmInput.addEventListener('input', () => {
            goBtn.disabled = confirmInput.value !== 'UNINSTALL';
        });
        overlay.querySelector('#uninst-cancel').addEventListener('click', () => overlay.remove());

        goBtn.addEventListener('click', async () => {
            goBtn.disabled = true;
            msg.textContent = 'Spawning cleanup script…';
            msg.style.color = 'var(--text-muted)';
            try {
                const result = await API.post('/api/settings/uninstall', {
                    remove_app:       overlay.querySelector('#uninst-app').checked,
                    remove_data:      overlay.querySelector('#uninst-data').checked,
                    remove_autostart: overlay.querySelector('#uninst-autostart').checked,
                    confirm: 'UNINSTALL',
                });
                overlay.querySelector('.modal-body').innerHTML = `
                    <h4 style="margin-top:0">Goodbye 👋</h4>
                    <p>PawPoller is shutting down. A cleanup script is running in the
                       background and will finish removing files after this process exits.</p>
                    <p style="font-size:12px;color:var(--text-muted)">
                        Actions queued:<br>${(result.actions || []).map(a => '• ' + Utils.escapeHtml(a)).join('<br>')}
                    </p>
                    <p style="font-size:12px;color:var(--text-muted)">
                        Server shutdown in ${result.shutdown_in_seconds || 2}s. Close this tab
                        when you're done.
                    </p>
                `;
                overlay.querySelector('.modal-footer').innerHTML =
                    '<button class="btn btn-secondary" id="uninst-close-tab">Close</button>';
                overlay.querySelector('#uninst-close-tab').addEventListener('click', () => overlay.remove());
            } catch (e) {
                msg.textContent = 'Uninstall failed: ' + e.message;
                msg.style.color = 'var(--danger)';
                goBtn.disabled = false;
            }
        });
    },

    /* Settings → Platforms: reorder the platform accordions alphabetically and
     * decorate each summary with its brand logo + a centred title. Called after
     * every renderSettings() paint (the pane is rebuilt fresh each time, so the
     * ordering/decoration must be re-applied). Session health stays pinned first;
     * only the platform accordions move. Cosmetic — wrapped so it can never break
     * Settings. */
    _enhancePlatformSettings() {
        try {
            const pane = document.querySelector('.settings-tab-content[data-tab-content="platforms"]');
            if (!pane) return;
            const accs = Array.from(pane.querySelectorAll(':scope > details.settings-accordion'));
            const platforms = accs
                .filter(a => !a.querySelector('#session-health-dot'))   // exclude the Session-health card
                .map(a => {
                    const summary = a.querySelector(':scope > summary');
                    const name = this._accordionName(summary);
                    const p = (window.PLATFORMS || []).find(x => x.label === name) || null;
                    return { el: a, summary, name, p };
                })
                .filter(o => o.summary && o.name);
            if (!platforms.length) return;
            platforms.sort((x, y) => x.name.localeCompare(y.name, undefined, { sensitivity: 'base' }));
            platforms.forEach(o => {
                pane.appendChild(o.el);                // re-append in sorted order (after Session health)
                this._decoratePlatformSummary(o.summary, o.p);
            });
            this._appendPlatformsFooter(pane);         // accounts link + trademark note, always last
            this._focusPlatformFromHash(pane);         // deep link from the setup wizard
        } catch (e) { /* cosmetic — never break Settings */ }
    },

    /* Open and scroll to one platform's accordion, from #/settings/platforms/<code>.
     *
     * The setup wizard's Connect button links here. Without this the user lands
     * on a wall of nineteen collapsed accordions and has to find their platform
     * by hand, which is barely better than the old behaviour of opening the
     * platform's website and capturing nothing.
     *
     * Runs at the END of _enhancePlatformSettings because that method re-appends
     * the accordions in sorted order — scrolling before the sort would target
     * the element's old position. */
    _focusPlatformFromHash(pane) {
        const code = (window.location.hash.match(/^#\/settings\/platforms\/([\w-]+)/) || [])[1];
        if (!code) return;
        // No escaping needed, and CSS.escape would be WRONG here: it escapes for
        // an identifier, so a digit-leading code becomes ` abc` — valid CSS
        // that never matches inside a quoted attribute value. The regex above
        // already limits `code` to [\w-]+, which cannot contain a quote or
        // backslash, so interpolating it directly is safe.
        const target = pane.querySelector(`details.settings-accordion[data-platform="${code}"]`);
        if (!target) return;                            // unknown code — leave the page alone
        target.open = true;
        // rAF so the scroll happens after the browser has laid out the now-open
        // accordion; otherwise it scrolls to where the collapsed element was.
        requestAnimationFrame(() => {
            target.scrollIntoView({ behavior: 'smooth', block: 'start' });
            target.classList.add('settings-accordion-flash');
            setTimeout(() => target.classList.remove('settings-accordion-flash'), 2000);
        });
    },

    /* Footer under the platform accordions: a pointer to the Accounts page for
     * multi-account users (these creds = the primary account) + a trademark
     * disclaimer for the brand logos. Idempotent — re-appended last each paint. */
    _appendPlatformsFooter(pane) {
        let foot = pane.querySelector('#pset-platforms-footer');
        if (!foot) {
            foot = document.createElement('div');
            foot.id = 'pset-platforms-footer';
            foot.className = 'pset-footer';
            foot.innerHTML =
                '<div class="pset-accounts-note">' +
                    '<div class="pset-accounts-text">' +
                        '<strong>Managing more than one account?</strong> ' +
                        'The credentials configured above are treated as your <strong>primary</strong> account ' +
                        'for each platform. Add extra accounts and switch between them on the Accounts page.' +
                    '</div>' +
                    '<a class="btn btn-secondary pset-accounts-btn" href="#/accounts">Manage accounts →</a>' +
                '</div>' +
                '<p class="pset-trademark">Platform names and logos are trademarks of their respective owners, ' +
                'shown here for identification only. PawPoller is not affiliated with, endorsed by, or sponsored ' +
                'by any of these platforms.</p>';
        }
        pane.appendChild(foot);                        // keep after the re-appended accordions
    },

    /* The platform name from an accordion summary (drops the dot / meta / logo). */
    _accordionName(summary) {
        if (!summary) return '';
        const clone = summary.cloneNode(true);
        clone.querySelectorAll('.status-dot, .summary-meta, .pset-logo, .pset-emoji').forEach(n => n.remove());
        return clone.textContent.trim();
    },

    /* Add the centred-title class + brand logo (emoji fallback if the file 404s). */
    _decoratePlatformSummary(summary, p) {
        if (!summary) return;
        summary.classList.add('pset-summary');
        if (summary.querySelector('.pset-logo, .pset-emoji')) return;   // idempotent
        if (!p || !p.logo) return;
        const dot = summary.querySelector('.status-dot');
        const logo = document.createElement('img');
        logo.className = 'pset-logo';
        logo.src = p.logo;
        logo.alt = '';
        logo.loading = 'lazy';
        logo.addEventListener('error', () => {
            const em = document.createElement('span');
            em.className = 'pset-emoji';
            em.textContent = p.emoji || '';
            logo.replaceWith(em);
        });
        if (dot && dot.parentNode === summary) summary.insertBefore(logo, dot.nextSibling);
        else summary.insertBefore(logo, summary.firstChild);
    },

    async renderSettings() {
        this._loading();
        try {
            // Core settings: only fetch what General/Platforms/Telegram/Data/About tabs need.
            // Polling tab data is loaded lazily when the user clicks into it.
            const [creds, prefs, telegram, tgFeatures, pollPausedState, faAuth, wsAuth, sfAuth, sqwAuth, ao3Auth, daAuth, wpAuth, ikAuth, bskyAuth, twAuth, mastAuth, tumAuth, pixAuth, thrAuth, igAuth, e621Auth, updateInfo, postingSettings, browserLoginInfo, setupStatus, digest, tgChannel, fnAuth, fbrAuth] = await Promise.all([
                API.getCredentials(),
                API.getPreferences(),
                API.getTelegram(),
                API.getTelegramFeatures().catch(() => ({ poll_summaries: true, error_alerts: true, milestones: true, digest: true, digest_interval_hours: 6 })),
                API.getPollPaused().catch(() => ({ polling_paused: false })),
                API.getFAAuthStatus().catch(() => ({ has_cookies: false })),
                API.getWSAuthStatus().catch(() => ({ has_key: false })),
                API.getSFAuthStatus().catch(() => ({ has_credentials: false })),
                API.getSQWAuthStatus().catch(() => ({ has_credentials: false })),
                API.getAO3AuthStatus().catch(() => ({ has_credentials: false })),
                API.getDAAuthStatus().catch(() => ({ has_credentials: false })),
                API.getWPAuthStatus().catch(() => ({ has_credentials: false })),
                API.getIKAuthStatus().catch(() => ({ has_credentials: false })),
                API.getBSKYAuthStatus().catch(() => ({ has_credentials: false })),
                API.getTWAuthStatus().catch(() => ({ has_credentials: false })),
                API.getMASTAuthStatus().catch(() => ({ has_credentials: false })),
                API.getTUMAuthStatus().catch(() => ({ has_credentials: false })),
                API.getPIXAuthStatus().catch(() => ({ has_credentials: false })),
                API.getTHRAuthStatus().catch(() => ({ has_credentials: false })),
                API.getIGAuthStatus().catch(() => ({ has_credentials: false })),
                API.getE621AuthStatus().catch(() => ({ has_credentials: false })),
                API.checkUpdate().catch(() => ({ available: false, current: '?', latest: '?' })),
                API.getPostingSettings().catch(() => ({ posting_enabled: false, posting_default_platforms: [], posting_default_rating: 'adult', posting_server_url: '', posting_server_api_key: '', posting_story_archive_path: '' })),
                API.getBrowserLoginPlatforms().catch(() => ({ available: false, platforms: [] })),
                API.getSetupStatus().catch(() => ({ runtime_mode: 'desktop', setup_mode: null, polling_owner: 'local' })),
                API.getDigestStatus().catch(() => ({ enabled: false, interval_days: 7, recipients: [], recipients_raw: '', smtp_host: 'smtp.gmail.com', smtp_port: 587, smtp_username: '', smtp_from: '', smtp_use_tls: true, has_password: false, last_sent_at: null })),
                API.getTelegramChannel().catch(() => ({ channel: '', has_own_token: false, uses_notification_bot: false, configured: false })),
                API.getFNAuthStatus().catch(() => ({ has_credentials: false, username: '' })),
                API.getFBRAuthStatus().catch(() => ({ has_credentials: false, username: '' })),
            ]);

            // Resolve effective mode for hide/show logic. Falls back to inferred
            // values when setup_mode is unset (existing installs from < 2.14.6).
            const _runtimeMode = setupStatus.runtime_mode || 'desktop';
            const _setupMode = setupStatus.setup_mode
                || (_runtimeMode === 'server' ? 'server'
                    : (postingSettings.posting_server_url ? 'paired_desktop' : 'standalone'));
            const _isServer = _runtimeMode === 'server';
            const _isPaired = _setupMode === 'paired_desktop';
            const _pollingOwner = setupStatus.polling_owner || (_isServer ? 'local' : (_isPaired ? 'server' : 'local'));

            // Store auth state for lazy-loaded polling tab
            this._pollingAuth = { faAuth, wsAuth, sfAuth, sqwAuth, ao3Auth, daAuth, wpAuth, ikAuth, bskyAuth, twAuth, mastAuth, tumAuth, pixAuth, thrAuth, igAuth, e621Auth, fnAuth, fbrAuth };

            // Store browser login availability for platform connect forms
            const _browserLoginAvailable = browserLoginInfo.available;

            let _settingsTab = (window.location.hash.match(/^#\/settings\/(\w+)/) || [])[1] || 'general';
            // Publishing tab folded into General (2.116.0) — redirect old deep links.
            if (_settingsTab === 'publishing') _settingsTab = 'general';

            const html = `
                <div class="page-header">
                    <h2>Settings</h2>
                    <div style="display:flex;gap:8px">
                        <button class="btn btn-success" id="save-all-settings-btn" title="Save all settings on this page">Save Settings</button>
                        <button class="btn btn-primary" id="poll-now-btn">Poll Now</button>
                        <button class="btn btn-secondary" id="full-resync-btn" title="Re-scrape all faves and comments">Full Resync</button>
                        <button class="btn btn-secondary" id="clear-session-btn" title="Clear cached API session">Clear Session</button>
                    </div>
                </div>

                <div class="settings-search-bar">
                    <input type="search" id="settings-search" class="settings-search-input"
                           placeholder="🔍 Search settings…" autocomplete="off"
                           aria-label="Search all settings">
                    <span id="settings-search-info" class="settings-search-info"></span>
                </div>

                <div class="settings-tabs" id="settings-tabs">
                    <button class="settings-tab ${_settingsTab === 'general' ? 'active' : ''}" data-stab="general">General</button>
                    <button class="settings-tab ${_settingsTab === 'appearance' ? 'active' : ''}" data-stab="appearance">Appearance</button>
                    <button class="settings-tab ${_settingsTab === 'platforms' ? 'active' : ''}" data-stab="platforms">Platforms</button>
                    <button class="settings-tab ${_settingsTab === 'polling' ? 'active' : ''}" data-stab="polling">Polling</button>
                    <button class="settings-tab ${_settingsTab === 'telegram' ? 'active' : ''}" data-stab="telegram">Telegram</button>
                    <button class="settings-tab ${_settingsTab === 'digest' ? 'active' : ''}" data-stab="digest">Weekly digest</button>
                    <button class="settings-tab ${_settingsTab === 'data' ? 'active' : ''}" data-stab="data">Data</button>
                    <button class="settings-tab ${_settingsTab === 'logs' ? 'active' : ''}" data-stab="logs">Logs</button>
                    <button class="settings-tab ${_settingsTab === 'about' ? 'active' : ''}" data-stab="about">About</button>
                    <button class="settings-tab ${_settingsTab === 'security' ? 'active' : ''}" data-stab="security">Security</button>
                    <button class="settings-tab ${_settingsTab === 'diagnostics' ? 'active' : ''}" data-stab="diagnostics">Diagnostics</button>
                </div>

                <!-- ═══ TAB: Diagnostics ═══ -->
                <div class="settings-tab-content" data-tab-content="diagnostics" ${_settingsTab !== 'diagnostics' ? 'style="display:none"' : ''}>
                    <div id="diagnostics-mount">
                        <div class="diagnostics-loading" style="padding:24px;color:var(--text-muted)">
                            Loading diagnostics suite…
                        </div>
                    </div>
                </div>

                <!-- ═══ TAB: Appearance ═══ -->
                <div class="settings-tab-content" data-tab-content="appearance" ${_settingsTab !== 'appearance' ? 'style="display:none"' : ''}>
                <div class="settings-section">
                    <h3>Theme</h3>
                    <p style="color:var(--text-muted);font-size:13px;margin-bottom:16px">
                        Click any theme to apply it instantly. The choice persists across sessions and (when cloud sync is enabled) syncs to other devices.
                    </p>
                    <div class="theme-picker" id="theme-picker">
                        ${this.THEMES.map(t => {
                            const isActive = t.id === this.getCurrentTheme();
                            const [bg, card, accent, accentWarm, text] = t.swatch;
                            return `
                              <div class="theme-card ${isActive ? 'active' : ''}" data-theme-id="${t.id}" role="button" tabindex="0" aria-label="Apply ${Utils.escapeHtml(t.name)} theme">
                                <div class="theme-card-preview">
                                  <div class="preview-bg" style="background:${bg}"></div>
                                  <div class="preview-card" style="background:${card};border-color:${text}22"></div>
                                  <div class="preview-warm" style="background:${accentWarm}"></div>
                                  <div class="preview-accent" style="background:${accent}"></div>
                                </div>
                                <div class="theme-card-meta">
                                  <div class="name">${Utils.escapeHtml(t.name)}</div>
                                  <div class="desc">${Utils.escapeHtml(t.desc)}</div>
                                  ${isActive ? '<span class="active-pill">Active</span>' : ''}
                                </div>
                              </div>
                            `;
                        }).join('')}
                    </div>
                </div>

                <div class="settings-section">
                    <h3>Display mode</h3>
                    <p style="color:var(--text-muted);font-size:13px;margin-bottom:16px">
                        A character layer on top of your theme. <strong>Default</strong> is the soft editorial look. <strong>Brut</strong> is neo-brutalist &mdash; thick ink borders, hard drop-shadows, bold-sans headings. It keeps your current theme's colours, so it works in warm paper or any dark theme. <span style="opacity:.75">(A green-on-black <em>Terminal</em> mode lives in the headless operator surface, not here.)</span>
                    </p>
                    <div class="mobile-mode-picker" id="display-mode-picker">
                        ${[
                            { id: 'default', name: 'Default', desc: 'Soft editorial — rounded cards, quiet shadows' },
                            { id: 'brut',    name: 'Brut', desc: 'Neo-brutalist — ink borders, hard shadows, bold sans' },
                        ].map(opt => {
                            const isActive = opt.id === this.getModeOverride();
                            return `
                              <div class="mobile-mode-card ${isActive ? 'active' : ''}" data-mode-id="${opt.id}" role="button" tabindex="0">
                                <div class="mobile-mode-name">${opt.name}</div>
                                <div class="mobile-mode-desc">${opt.desc}</div>
                                ${isActive ? '<span class="active-pill">Active</span>' : ''}
                              </div>
                            `;
                        }).join('')}
                    </div>
                </div>

                <div class="settings-section">
                    <h3>Navigation</h3>
                    <p style="color:var(--text-muted);font-size:13px;margin-bottom:16px">
                        Where the main navigation lives. <strong>Top bar</strong> is the redesigned shell &mdash; a horizontal bar with the Publishing / Insights groups as dropdowns. <strong>Side rail</strong> is the classic left sidebar. Applies on desktop; phones keep the bottom nav either way.
                    </p>
                    <div class="mobile-mode-picker" id="nav-mode-picker">
                        ${[
                            { id: 'top',  name: 'Top bar',  desc: 'Horizontal nav across the top (redesign)' },
                            { id: 'side', name: 'Side rail', desc: 'Classic labelled left sidebar' },
                        ].map(opt => {
                            const isActive = opt.id === this.getNavModeOverride();
                            return `
                              <div class="mobile-mode-card ${isActive ? 'active' : ''}" data-nav-id="${opt.id}" role="button" tabindex="0">
                                <div class="mobile-mode-name">${opt.name}</div>
                                <div class="mobile-mode-desc">${opt.desc}</div>
                                ${isActive ? '<span class="active-pill">Active</span>' : ''}
                              </div>
                            `;
                        }).join('')}
                    </div>
                </div>

                <div class="settings-section">
                    <h3>Mobile Layout</h3>
                    <p style="color:var(--text-muted);font-size:13px;margin-bottom:16px">
                        Auto-detect uses the mobile interface on screens 768px wide or less (rotates with the viewport). Force on uses it everywhere — handy for testing or if you'd rather have the touch-first layout on a tablet. Force off keeps the desktop UX even on a phone (best-effort: some legacy responsive rules still fire on small screens).
                    </p>
                    <div class="mobile-mode-picker" id="mobile-mode-picker">
                        ${[
                            { id: 'auto', name: 'Auto', desc: 'Follows screen size (recommended)' },
                            { id: 'on',   name: 'Always on', desc: 'Mobile interface on every device' },
                            { id: 'off',  name: 'Always off', desc: 'Desktop interface even on a phone' },
                        ].map(opt => {
                            const isActive = opt.id === this.getMobileModeOverride();
                            return `
                              <div class="mobile-mode-card ${isActive ? 'active' : ''}" data-mm-id="${opt.id}" role="button" tabindex="0">
                                <div class="mobile-mode-name">${opt.name}</div>
                                <div class="mobile-mode-desc">${opt.desc}</div>
                                ${isActive ? '<span class="active-pill">Active</span>' : ''}
                              </div>
                            `;
                        }).join('')}
                    </div>
                    <div style="margin-top:10px;font-size:11px;color:var(--text-muted)">
                        Currently rendering: <strong>${this.isMobileLayoutActive() ? 'Mobile' : 'Desktop'}</strong> layout
                    </div>
                </div>

                <div class="settings-section">
                    <h3>Sync</h3>
                    <p style="color:var(--text-muted);font-size:13px;margin-bottom:16px">
                        When enabled, this device pushes preference changes to your cloud server within seconds and pulls remote changes every 5 minutes. Browser tabs also refresh on focus. Credentials and your session secret are excluded.
                    </p>
                    <div class="settings-row">
                        <div>
                            <span class="settings-label">Auto-sync settings across devices</span>
                            <div style="font-size:11px;color:var(--text-muted);margin-top:2px">Requires <code>posting_server_url</code> + API key in the Publishing section below</div>
                        </div>
                        <label class="toggle-switch">
                            <input type="checkbox" id="pref-auto-sync" ${prefs.auto_sync_enabled !== false ? 'checked' : ''}>
                            <span class="toggle-slider"></span>
                        </label>
                    </div>
                </div>
                </div>

                <!-- ═══ TAB: General ═══ -->
                <div class="settings-tab-content" data-tab-content="general" ${_settingsTab !== 'general' ? 'style="display:none"' : ''}>

                <details class="settings-accordion" open>
                    <summary>Setup Mode <span class="summary-meta">— ${_isServer ? 'Server (Docker)' : (_isPaired ? 'Paired with server' : 'Standalone')}</span></summary>
                    <div class="accordion-body">
                    <div class="settings-row">
                        <div>
                            <span class="settings-label">${_isServer ? 'This is the server.' : (_isPaired ? 'Paired with a remote server.' : 'Standalone — running locally only.')}</span>
                            <div style="font-size:11px;color:var(--text-muted);margin-top:2px">
                                Polling owner: <strong>${_pollingOwner === 'local' ? (_isServer ? 'this server' : 'this computer') : 'remote server'}</strong>
                                ${_isPaired && postingSettings.posting_server_url ? `&middot; Server: <code>${Utils.escapeHtml(postingSettings.posting_server_url)}</code>` : ''}
                            </div>
                        </div>
                        ${_isServer ? '' : '<button class="btn btn-secondary" id="btn-rerun-wizard" title="Switch between standalone and paired modes">Re-run setup</button>'}
                    </div>
                    </div>
                </details>

                ${_isPaired ? `
                <details class="settings-accordion" open id="mirror-sync-section">
                    <summary>Sync with server <span class="summary-meta" id="mirror-sync-badge">— checking…</span></summary>
                    <div class="accordion-body" id="mirror-sync-body">
                        <p style="color:var(--text-muted);font-size:13px">Loading sync status…</p>
                    </div>
                </details>` : ''}

                <details class="settings-accordion" open>
                    <summary>App Preferences</summary>
                    <div class="accordion-body">
                    ${_isServer ? '' : `
                    <div class="settings-row">
                        <div>
                            <span class="settings-label">Minimize to system tray on close</span>
                            <div style="font-size:11px;color:var(--text-muted);margin-top:2px">Hide to tray instead of quitting — poller keeps running</div>
                        </div>
                        <label class="toggle-switch">
                            <input type="checkbox" id="pref-tray" ${prefs.minimize_to_tray ? 'checked' : ''}>
                            <span class="toggle-slider"></span>
                        </label>
                    </div>
                    <div class="settings-row">
                        <div>
                            <span class="settings-label">Start with Windows</span>
                            <div style="font-size:11px;color:var(--text-muted);margin-top:2px">Launch automatically when you log in</div>
                        </div>
                        <label class="toggle-switch">
                            <input type="checkbox" id="pref-startup" ${prefs.run_on_startup ? 'checked' : ''}>
                            <span class="toggle-slider"></span>
                        </label>
                    </div>
                    <div class="settings-row">
                        <div>
                            <span class="settings-label">Desktop notifications</span>
                            <div style="font-size:11px;color:var(--text-muted);margin-top:2px">Toast notifications when new faves or comments are detected</div>
                        </div>
                        <label class="toggle-switch">
                            <input type="checkbox" id="pref-notifications" ${prefs.notifications_enabled ? 'checked' : ''}>
                            <span class="toggle-slider"></span>
                        </label>
                    </div>
                    `}
                    <div class="settings-row">
                        <div>
                            <span class="settings-label">Watcher notifications</span>
                            <div style="font-size:11px;color:var(--text-muted);margin-top:2px">Toast + Telegram alerts for new watchers</div>
                        </div>
                        <label class="toggle-switch">
                            <input type="checkbox" id="pref-watcher-notif" ${prefs.watcher_notifications_enabled !== false ? 'checked' : ''}>
                            <span class="toggle-slider"></span>
                        </label>
                    </div>
                    <div class="settings-row">
                        <div>
                            <span class="settings-label">Floating logs button</span>
                            <div style="font-size:11px;color:var(--text-muted);margin-top:2px">Show the bottom-right "Logs" button that live-tails server/app/polling logs</div>
                        </div>
                        <label class="toggle-switch">
                            <input type="checkbox" id="pref-logs-panel" ${prefs.logs_panel_enabled !== false ? 'checked' : ''}>
                            <span class="toggle-slider"></span>
                        </label>
                    </div>
                    </div>
                </details>

                <details class="settings-accordion">
                    <summary>Poll Intervals <span class="summary-meta">— controls how often each platform is checked</span></summary>
                    <div class="accordion-body">
                    <div class="settings-row" style="border-bottom:2px solid var(--border);padding-bottom:14px;margin-bottom:6px">
                        <div>
                            <span class="settings-label">Set all platforms</span>
                            <div style="font-size:11px;color:var(--text-muted);margin-top:2px">Apply one interval to every platform at once</div>
                        </div>
                        <select class="filter-select" id="pref-poll-interval-all" style="width:auto">
                            <option value="">— choose —</option>
                            <option value="15">15 min</option>
                            <option value="30">30 min</option>
                            <option value="60">1 hour</option>
                            <option value="120">2 hours</option>
                            <option value="240">4 hours</option>
                            <option value="360">6 hours</option>
                            <option value="480">8 hours</option>
                            <option value="600">10 hours</option>
                            <option value="720">12 hours</option>
                        </select>
                    </div>
                    <div class="settings-row">
                        <div>
                            <span class="settings-label">IB poll interval</span>
                            <div style="font-size:11px;color:var(--text-muted);margin-top:2px">How often to check for new Inkbunny data</div>
                        </div>
                        <select class="filter-select" id="pref-poll-interval" style="width:auto">
                            <option value="15" ${prefs.poll_interval_minutes === 15 ? 'selected' : ''}>15 min</option>
                            <option value="30" ${prefs.poll_interval_minutes === 30 ? 'selected' : ''}>30 min</option>
                            <option value="60" ${prefs.poll_interval_minutes === 60 || !prefs.poll_interval_minutes ? 'selected' : ''}>1 hour</option>
                            <option value="120" ${prefs.poll_interval_minutes === 120 ? 'selected' : ''}>2 hours</option>
                            <option value="240" ${prefs.poll_interval_minutes === 240 ? 'selected' : ''}>4 hours</option>
                            <option value="360" ${prefs.poll_interval_minutes === 360 ? 'selected' : ''}>6 hours</option>
                            <option value="480" ${prefs.poll_interval_minutes === 480 ? 'selected' : ''}>8 hours</option>
                            <option value="600" ${prefs.poll_interval_minutes === 600 ? 'selected' : ''}>10 hours</option>
                            <option value="720" ${prefs.poll_interval_minutes === 720 ? 'selected' : ''}>12 hours</option>
                        </select>
                    </div>
                    <div class="settings-row">
                        <div>
                            <span class="settings-label">FA poll interval</span>
                            <div style="font-size:11px;color:var(--text-muted);margin-top:2px">How often to check for new FurAffinity data</div>
                        </div>
                        <select class="filter-select" id="pref-fa-poll-interval" style="width:auto">
                            <option value="15" ${prefs.fa_poll_interval_minutes === 15 ? 'selected' : ''}>15 min</option>
                            <option value="30" ${prefs.fa_poll_interval_minutes === 30 ? 'selected' : ''}>30 min</option>
                            <option value="60" ${prefs.fa_poll_interval_minutes === 60 || !prefs.fa_poll_interval_minutes ? 'selected' : ''}>1 hour</option>
                            <option value="120" ${prefs.fa_poll_interval_minutes === 120 ? 'selected' : ''}>2 hours</option>
                            <option value="240" ${prefs.fa_poll_interval_minutes === 240 ? 'selected' : ''}>4 hours</option>
                            <option value="360" ${prefs.fa_poll_interval_minutes === 360 ? 'selected' : ''}>6 hours</option>
                            <option value="480" ${prefs.fa_poll_interval_minutes === 480 ? 'selected' : ''}>8 hours</option>
                            <option value="600" ${prefs.fa_poll_interval_minutes === 600 ? 'selected' : ''}>10 hours</option>
                            <option value="720" ${prefs.fa_poll_interval_minutes === 720 ? 'selected' : ''}>12 hours</option>
                        </select>
                    </div>
                    <div class="settings-row">
                        <div>
                            <span class="settings-label">WS poll interval</span>
                            <div style="font-size:11px;color:var(--text-muted);margin-top:2px">How often to check for new Weasyl data</div>
                        </div>
                        <select class="filter-select" id="pref-ws-poll-interval" style="width:auto">
                            <option value="15" ${prefs.ws_poll_interval_minutes === 15 ? 'selected' : ''}>15 min</option>
                            <option value="30" ${prefs.ws_poll_interval_minutes === 30 ? 'selected' : ''}>30 min</option>
                            <option value="60" ${prefs.ws_poll_interval_minutes === 60 || !prefs.ws_poll_interval_minutes ? 'selected' : ''}>1 hour</option>
                            <option value="120" ${prefs.ws_poll_interval_minutes === 120 ? 'selected' : ''}>2 hours</option>
                            <option value="240" ${prefs.ws_poll_interval_minutes === 240 ? 'selected' : ''}>4 hours</option>
                            <option value="360" ${prefs.ws_poll_interval_minutes === 360 ? 'selected' : ''}>6 hours</option>
                            <option value="480" ${prefs.ws_poll_interval_minutes === 480 ? 'selected' : ''}>8 hours</option>
                            <option value="600" ${prefs.ws_poll_interval_minutes === 600 ? 'selected' : ''}>10 hours</option>
                            <option value="720" ${prefs.ws_poll_interval_minutes === 720 ? 'selected' : ''}>12 hours</option>
                        </select>
                    </div>
                    <div class="settings-row">
                        <div>
                            <span class="settings-label">SF poll interval</span>
                            <div style="font-size:11px;color:var(--text-muted);margin-top:2px">How often to check for new SoFurry data</div>
                        </div>
                        <select class="filter-select" id="pref-sf-poll-interval" style="width:auto">
                            <option value="15" ${prefs.sf_poll_interval_minutes === 15 ? 'selected' : ''}>15 min</option>
                            <option value="30" ${prefs.sf_poll_interval_minutes === 30 ? 'selected' : ''}>30 min</option>
                            <option value="60" ${prefs.sf_poll_interval_minutes === 60 || !prefs.sf_poll_interval_minutes ? 'selected' : ''}>1 hour</option>
                            <option value="120" ${prefs.sf_poll_interval_minutes === 120 ? 'selected' : ''}>2 hours</option>
                            <option value="240" ${prefs.sf_poll_interval_minutes === 240 ? 'selected' : ''}>4 hours</option>
                            <option value="360" ${prefs.sf_poll_interval_minutes === 360 ? 'selected' : ''}>6 hours</option>
                            <option value="480" ${prefs.sf_poll_interval_minutes === 480 ? 'selected' : ''}>8 hours</option>
                            <option value="600" ${prefs.sf_poll_interval_minutes === 600 ? 'selected' : ''}>10 hours</option>
                            <option value="720" ${prefs.sf_poll_interval_minutes === 720 ? 'selected' : ''}>12 hours</option>
                        </select>
                    </div>
                    <div class="settings-row">
                        <div>
                            <span class="settings-label">SqW poll interval</span>
                            <div style="font-size:11px;color:var(--text-muted);margin-top:2px">How often to check for new SquidgeWorld data</div>
                        </div>
                        <select class="filter-select" id="pref-sqw-poll-interval" style="width:auto">
                            <option value="15" ${prefs.sqw_poll_interval_minutes === 15 ? 'selected' : ''}>15 min</option>
                            <option value="30" ${prefs.sqw_poll_interval_minutes === 30 ? 'selected' : ''}>30 min</option>
                            <option value="60" ${prefs.sqw_poll_interval_minutes === 60 || !prefs.sqw_poll_interval_minutes ? 'selected' : ''}>1 hour</option>
                            <option value="120" ${prefs.sqw_poll_interval_minutes === 120 ? 'selected' : ''}>2 hours</option>
                            <option value="240" ${prefs.sqw_poll_interval_minutes === 240 ? 'selected' : ''}>4 hours</option>
                            <option value="360" ${prefs.sqw_poll_interval_minutes === 360 ? 'selected' : ''}>6 hours</option>
                            <option value="480" ${prefs.sqw_poll_interval_minutes === 480 ? 'selected' : ''}>8 hours</option>
                            <option value="600" ${prefs.sqw_poll_interval_minutes === 600 ? 'selected' : ''}>10 hours</option>
                            <option value="720" ${prefs.sqw_poll_interval_minutes === 720 ? 'selected' : ''}>12 hours</option>
                        </select>
                    </div>
                    <div class="settings-row">
                        <div>
                            <span class="settings-label">AO3 poll interval</span>
                            <div style="font-size:11px;color:var(--text-muted);margin-top:2px">How often to check for new AO3 data</div>
                        </div>
                        <select class="filter-select" id="pref-ao3-poll-interval" style="width:auto">
                            <option value="15" ${prefs.ao3_poll_interval_minutes === 15 ? 'selected' : ''}>15 min</option>
                            <option value="30" ${prefs.ao3_poll_interval_minutes === 30 ? 'selected' : ''}>30 min</option>
                            <option value="60" ${prefs.ao3_poll_interval_minutes === 60 || !prefs.ao3_poll_interval_minutes ? 'selected' : ''}>1 hour</option>
                            <option value="120" ${prefs.ao3_poll_interval_minutes === 120 ? 'selected' : ''}>2 hours</option>
                            <option value="240" ${prefs.ao3_poll_interval_minutes === 240 ? 'selected' : ''}>4 hours</option>
                            <option value="360" ${prefs.ao3_poll_interval_minutes === 360 ? 'selected' : ''}>6 hours</option>
                            <option value="480" ${prefs.ao3_poll_interval_minutes === 480 ? 'selected' : ''}>8 hours</option>
                            <option value="600" ${prefs.ao3_poll_interval_minutes === 600 ? 'selected' : ''}>10 hours</option>
                            <option value="720" ${prefs.ao3_poll_interval_minutes === 720 ? 'selected' : ''}>12 hours</option>
                        </select>
                    </div>
                    <div class="settings-row">
                        <div>
                            <span class="settings-label">DA poll interval</span>
                            <div style="font-size:11px;color:var(--text-muted);margin-top:2px">How often to check for new DeviantArt data</div>
                        </div>
                        <select class="filter-select" id="pref-da-poll-interval" style="width:auto">
                            <option value="15" ${prefs.da_poll_interval_minutes === 15 ? 'selected' : ''}>15 min</option>
                            <option value="30" ${prefs.da_poll_interval_minutes === 30 ? 'selected' : ''}>30 min</option>
                            <option value="60" ${prefs.da_poll_interval_minutes === 60 || !prefs.da_poll_interval_minutes ? 'selected' : ''}>1 hour</option>
                            <option value="120" ${prefs.da_poll_interval_minutes === 120 ? 'selected' : ''}>2 hours</option>
                            <option value="240" ${prefs.da_poll_interval_minutes === 240 ? 'selected' : ''}>4 hours</option>
                            <option value="360" ${prefs.da_poll_interval_minutes === 360 ? 'selected' : ''}>6 hours</option>
                            <option value="480" ${prefs.da_poll_interval_minutes === 480 ? 'selected' : ''}>8 hours</option>
                            <option value="600" ${prefs.da_poll_interval_minutes === 600 ? 'selected' : ''}>10 hours</option>
                            <option value="720" ${prefs.da_poll_interval_minutes === 720 ? 'selected' : ''}>12 hours</option>
                        </select>
                    </div>
                    <div class="settings-row">
                        <div>
                            <span class="settings-label">WP poll interval</span>
                            <div style="font-size:11px;color:var(--text-muted);margin-top:2px">How often to check for new Wattpad data</div>
                        </div>
                        <select class="filter-select" id="pref-wp-poll-interval" style="width:auto">
                            <option value="15" ${prefs.wp_poll_interval_minutes === 15 ? 'selected' : ''}>15 min</option>
                            <option value="30" ${prefs.wp_poll_interval_minutes === 30 ? 'selected' : ''}>30 min</option>
                            <option value="60" ${prefs.wp_poll_interval_minutes === 60 || !prefs.wp_poll_interval_minutes ? 'selected' : ''}>1 hour</option>
                            <option value="120" ${prefs.wp_poll_interval_minutes === 120 ? 'selected' : ''}>2 hours</option>
                            <option value="240" ${prefs.wp_poll_interval_minutes === 240 ? 'selected' : ''}>4 hours</option>
                            <option value="360" ${prefs.wp_poll_interval_minutes === 360 ? 'selected' : ''}>6 hours</option>
                            <option value="480" ${prefs.wp_poll_interval_minutes === 480 ? 'selected' : ''}>8 hours</option>
                            <option value="600" ${prefs.wp_poll_interval_minutes === 600 ? 'selected' : ''}>10 hours</option>
                            <option value="720" ${prefs.wp_poll_interval_minutes === 720 ? 'selected' : ''}>12 hours</option>
                        </select>
                    </div>
                    <div class="settings-row">
                        <div>
                            <span class="settings-label">IK poll interval</span>
                            <div style="font-size:11px;color:var(--text-muted);margin-top:2px">How often to check for new Itaku data</div>
                        </div>
                        <select class="filter-select" id="pref-ik-poll-interval" style="width:auto">
                            <option value="15" ${prefs.ik_poll_interval_minutes === 15 ? 'selected' : ''}>15 min</option>
                            <option value="30" ${prefs.ik_poll_interval_minutes === 30 ? 'selected' : ''}>30 min</option>
                            <option value="60" ${prefs.ik_poll_interval_minutes === 60 || !prefs.ik_poll_interval_minutes ? 'selected' : ''}>1 hour</option>
                            <option value="120" ${prefs.ik_poll_interval_minutes === 120 ? 'selected' : ''}>2 hours</option>
                            <option value="240" ${prefs.ik_poll_interval_minutes === 240 ? 'selected' : ''}>4 hours</option>
                            <option value="360" ${prefs.ik_poll_interval_minutes === 360 ? 'selected' : ''}>6 hours</option>
                            <option value="480" ${prefs.ik_poll_interval_minutes === 480 ? 'selected' : ''}>8 hours</option>
                            <option value="600" ${prefs.ik_poll_interval_minutes === 600 ? 'selected' : ''}>10 hours</option>
                            <option value="720" ${prefs.ik_poll_interval_minutes === 720 ? 'selected' : ''}>12 hours</option>
                        </select>
                    </div>
                    <div class="settings-row">
                        <div>
                            <span class="settings-label">BSKY poll interval</span>
                            <div style="font-size:11px;color:var(--text-muted);margin-top:2px">How often to check for new Bluesky data</div>
                        </div>
                        <select class="filter-select" id="pref-bsky-poll-interval" style="width:auto">
                            <option value="15" ${prefs.bsky_poll_interval_minutes === 15 ? 'selected' : ''}>15 min</option>
                            <option value="30" ${prefs.bsky_poll_interval_minutes === 30 ? 'selected' : ''}>30 min</option>
                            <option value="60" ${prefs.bsky_poll_interval_minutes === 60 || !prefs.bsky_poll_interval_minutes ? 'selected' : ''}>1 hour</option>
                            <option value="120" ${prefs.bsky_poll_interval_minutes === 120 ? 'selected' : ''}>2 hours</option>
                            <option value="240" ${prefs.bsky_poll_interval_minutes === 240 ? 'selected' : ''}>4 hours</option>
                            <option value="360" ${prefs.bsky_poll_interval_minutes === 360 ? 'selected' : ''}>6 hours</option>
                            <option value="480" ${prefs.bsky_poll_interval_minutes === 480 ? 'selected' : ''}>8 hours</option>
                            <option value="600" ${prefs.bsky_poll_interval_minutes === 600 ? 'selected' : ''}>10 hours</option>
                            <option value="720" ${prefs.bsky_poll_interval_minutes === 720 ? 'selected' : ''}>12 hours</option>
                        </select>
                    </div>
                    <div class="settings-row">
                        <div>
                            <span class="settings-label">TW poll interval</span>
                            <div style="font-size:11px;color:var(--text-muted);margin-top:2px">How often to check for new X/Twitter data</div>
                        </div>
                        <select class="filter-select" id="pref-tw-poll-interval" style="width:auto">
                            <option value="15" ${prefs.tw_poll_interval_minutes === 15 ? 'selected' : ''}>15 min</option>
                            <option value="30" ${prefs.tw_poll_interval_minutes === 30 ? 'selected' : ''}>30 min</option>
                            <option value="60" ${prefs.tw_poll_interval_minutes === 60 || !prefs.tw_poll_interval_minutes ? 'selected' : ''}>1 hour</option>
                            <option value="120" ${prefs.tw_poll_interval_minutes === 120 ? 'selected' : ''}>2 hours</option>
                            <option value="240" ${prefs.tw_poll_interval_minutes === 240 ? 'selected' : ''}>4 hours</option>
                            <option value="360" ${prefs.tw_poll_interval_minutes === 360 ? 'selected' : ''}>6 hours</option>
                            <option value="480" ${prefs.tw_poll_interval_minutes === 480 ? 'selected' : ''}>8 hours</option>
                            <option value="600" ${prefs.tw_poll_interval_minutes === 600 ? 'selected' : ''}>10 hours</option>
                            <option value="720" ${prefs.tw_poll_interval_minutes === 720 ? 'selected' : ''}>12 hours</option>
                        </select>
                    </div>
                    <div class="settings-row">
                        <div>
                            <span class="settings-label">MAST poll interval</span>
                            <div style="font-size:11px;color:var(--text-muted);margin-top:2px">How often to check for new Mastodon data</div>
                        </div>
                        <select class="filter-select" id="pref-mast-poll-interval" style="width:auto">
                            <option value="15" ${prefs.mast_poll_interval_minutes === 15 ? 'selected' : ''}>15 min</option>
                            <option value="30" ${prefs.mast_poll_interval_minutes === 30 ? 'selected' : ''}>30 min</option>
                            <option value="60" ${prefs.mast_poll_interval_minutes === 60 || !prefs.mast_poll_interval_minutes ? 'selected' : ''}>1 hour</option>
                            <option value="120" ${prefs.mast_poll_interval_minutes === 120 ? 'selected' : ''}>2 hours</option>
                            <option value="240" ${prefs.mast_poll_interval_minutes === 240 ? 'selected' : ''}>4 hours</option>
                            <option value="360" ${prefs.mast_poll_interval_minutes === 360 ? 'selected' : ''}>6 hours</option>
                            <option value="480" ${prefs.mast_poll_interval_minutes === 480 ? 'selected' : ''}>8 hours</option>
                            <option value="600" ${prefs.mast_poll_interval_minutes === 600 ? 'selected' : ''}>10 hours</option>
                            <option value="720" ${prefs.mast_poll_interval_minutes === 720 ? 'selected' : ''}>12 hours</option>
                        </select>
                    </div>
                    <div class="settings-row">
                        <div>
                            <span class="settings-label">TUM poll interval</span>
                            <div style="font-size:11px;color:var(--text-muted);margin-top:2px">How often to check for new Tumblr data</div>
                        </div>
                        <select class="filter-select" id="pref-tum-poll-interval" style="width:auto">
                            <option value="15" ${prefs.tum_poll_interval_minutes === 15 ? 'selected' : ''}>15 min</option>
                            <option value="30" ${prefs.tum_poll_interval_minutes === 30 ? 'selected' : ''}>30 min</option>
                            <option value="60" ${prefs.tum_poll_interval_minutes === 60 || !prefs.tum_poll_interval_minutes ? 'selected' : ''}>1 hour</option>
                            <option value="120" ${prefs.tum_poll_interval_minutes === 120 ? 'selected' : ''}>2 hours</option>
                            <option value="240" ${prefs.tum_poll_interval_minutes === 240 ? 'selected' : ''}>4 hours</option>
                            <option value="360" ${prefs.tum_poll_interval_minutes === 360 ? 'selected' : ''}>6 hours</option>
                            <option value="480" ${prefs.tum_poll_interval_minutes === 480 ? 'selected' : ''}>8 hours</option>
                            <option value="600" ${prefs.tum_poll_interval_minutes === 600 ? 'selected' : ''}>10 hours</option>
                            <option value="720" ${prefs.tum_poll_interval_minutes === 720 ? 'selected' : ''}>12 hours</option>
                        </select>
                    </div>
                    <div class="settings-row">
                        <div>
                            <span class="settings-label">PIX poll interval</span>
                            <div style="font-size:11px;color:var(--text-muted);margin-top:2px">How often to check for new Pixiv data</div>
                        </div>
                        <select class="filter-select" id="pref-pix-poll-interval" style="width:auto">
                            <option value="15" ${prefs.pix_poll_interval_minutes === 15 ? 'selected' : ''}>15 min</option>
                            <option value="30" ${prefs.pix_poll_interval_minutes === 30 ? 'selected' : ''}>30 min</option>
                            <option value="60" ${prefs.pix_poll_interval_minutes === 60 || !prefs.pix_poll_interval_minutes ? 'selected' : ''}>1 hour</option>
                            <option value="120" ${prefs.pix_poll_interval_minutes === 120 ? 'selected' : ''}>2 hours</option>
                            <option value="240" ${prefs.pix_poll_interval_minutes === 240 ? 'selected' : ''}>4 hours</option>
                            <option value="360" ${prefs.pix_poll_interval_minutes === 360 ? 'selected' : ''}>6 hours</option>
                            <option value="480" ${prefs.pix_poll_interval_minutes === 480 ? 'selected' : ''}>8 hours</option>
                            <option value="600" ${prefs.pix_poll_interval_minutes === 600 ? 'selected' : ''}>10 hours</option>
                            <option value="720" ${prefs.pix_poll_interval_minutes === 720 ? 'selected' : ''}>12 hours</option>
                        </select>
                    </div>
                    <div class="settings-row">
                        <div>
                            <span class="settings-label">THR poll interval</span>
                            <div style="font-size:11px;color:var(--text-muted);margin-top:2px">How often to check for new Threads data</div>
                        </div>
                        <select class="filter-select" id="pref-thr-poll-interval" style="width:auto">
                            <option value="15" ${prefs.thr_poll_interval_minutes === 15 ? 'selected' : ''}>15 min</option>
                            <option value="30" ${prefs.thr_poll_interval_minutes === 30 ? 'selected' : ''}>30 min</option>
                            <option value="60" ${prefs.thr_poll_interval_minutes === 60 || !prefs.thr_poll_interval_minutes ? 'selected' : ''}>1 hour</option>
                            <option value="120" ${prefs.thr_poll_interval_minutes === 120 ? 'selected' : ''}>2 hours</option>
                            <option value="240" ${prefs.thr_poll_interval_minutes === 240 ? 'selected' : ''}>4 hours</option>
                            <option value="360" ${prefs.thr_poll_interval_minutes === 360 ? 'selected' : ''}>6 hours</option>
                            <option value="480" ${prefs.thr_poll_interval_minutes === 480 ? 'selected' : ''}>8 hours</option>
                            <option value="600" ${prefs.thr_poll_interval_minutes === 600 ? 'selected' : ''}>10 hours</option>
                            <option value="720" ${prefs.thr_poll_interval_minutes === 720 ? 'selected' : ''}>12 hours</option>
                        </select>
                    </div>
                    <div class="settings-row">
                        <div>
                            <span class="settings-label">IG poll interval</span>
                            <div style="font-size:11px;color:var(--text-muted);margin-top:2px">How often to check for new Instagram data</div>
                        </div>
                        <select class="filter-select" id="pref-ig-poll-interval" style="width:auto">
                            <option value="15" ${prefs.ig_poll_interval_minutes === 15 ? 'selected' : ''}>15 min</option>
                            <option value="30" ${prefs.ig_poll_interval_minutes === 30 ? 'selected' : ''}>30 min</option>
                            <option value="60" ${prefs.ig_poll_interval_minutes === 60 || !prefs.ig_poll_interval_minutes ? 'selected' : ''}>1 hour</option>
                            <option value="120" ${prefs.ig_poll_interval_minutes === 120 ? 'selected' : ''}>2 hours</option>
                            <option value="240" ${prefs.ig_poll_interval_minutes === 240 ? 'selected' : ''}>4 hours</option>
                            <option value="360" ${prefs.ig_poll_interval_minutes === 360 ? 'selected' : ''}>6 hours</option>
                            <option value="480" ${prefs.ig_poll_interval_minutes === 480 ? 'selected' : ''}>8 hours</option>
                            <option value="600" ${prefs.ig_poll_interval_minutes === 600 ? 'selected' : ''}>10 hours</option>
                            <option value="720" ${prefs.ig_poll_interval_minutes === 720 ? 'selected' : ''}>12 hours</option>
                        </select>
                    </div>
                    <div class="settings-row">
                        <div>
                            <span class="settings-label">e621 poll interval</span>
                            <div style="font-size:11px;color:var(--text-muted);margin-top:2px">How often to check for new e621 data</div>
                        </div>
                        <select class="filter-select" id="pref-e621-poll-interval" style="width:auto">
                            <option value="15" ${prefs.e621_poll_interval_minutes === 15 ? 'selected' : ''}>15 min</option>
                            <option value="30" ${prefs.e621_poll_interval_minutes === 30 ? 'selected' : ''}>30 min</option>
                            <option value="60" ${prefs.e621_poll_interval_minutes === 60 || !prefs.e621_poll_interval_minutes ? 'selected' : ''}>1 hour</option>
                            <option value="120" ${prefs.e621_poll_interval_minutes === 120 ? 'selected' : ''}>2 hours</option>
                            <option value="240" ${prefs.e621_poll_interval_minutes === 240 ? 'selected' : ''}>4 hours</option>
                            <option value="360" ${prefs.e621_poll_interval_minutes === 360 ? 'selected' : ''}>6 hours</option>
                            <option value="480" ${prefs.e621_poll_interval_minutes === 480 ? 'selected' : ''}>8 hours</option>
                            <option value="600" ${prefs.e621_poll_interval_minutes === 600 ? 'selected' : ''}>10 hours</option>
                            <option value="720" ${prefs.e621_poll_interval_minutes === 720 ? 'selected' : ''}>12 hours</option>
                        </select>
                    </div>
                    <div class="settings-row">
                        <div>
                            <span class="settings-label">Display timezone</span>
                            <div style="font-size:11px;color:var(--text-muted);margin-top:2px">Timezone for Telegram messages and timestamps</div>
                        </div>
                        <select class="filter-select" id="pref-timezone" style="width:auto">
                            ${[
                                ['UTC', 'UTC'],
                                ['Australia/Sydney', 'Sydney (AEST/AEDT)'],
                                ['Australia/Melbourne', 'Melbourne (AEST/AEDT)'],
                                ['Australia/Brisbane', 'Brisbane (AEST)'],
                                ['Australia/Adelaide', 'Adelaide (ACST/ACDT)'],
                                ['Australia/Perth', 'Perth (AWST)'],
                                ['Australia/Darwin', 'Darwin (ACST)'],
                                ['Australia/Hobart', 'Hobart (AEST/AEDT)'],
                                ['Pacific/Auckland', 'Auckland (NZST/NZDT)'],
                                ['Asia/Tokyo', 'Tokyo (JST)'],
                                ['Asia/Singapore', 'Singapore (SGT)'],
                                ['Asia/Hong_Kong', 'Hong Kong (HKT)'],
                                ['Asia/Kolkata', 'India (IST)'],
                                ['Europe/London', 'London (GMT/BST)'],
                                ['Europe/Paris', 'Paris (CET/CEST)'],
                                ['Europe/Berlin', 'Berlin (CET/CEST)'],
                                ['America/New_York', 'New York (EST/EDT)'],
                                ['America/Chicago', 'Chicago (CST/CDT)'],
                                ['America/Denver', 'Denver (MST/MDT)'],
                                ['America/Los_Angeles', 'Los Angeles (PST/PDT)'],
                            ].map(([val, label]) => `<option value="${val}" ${prefs.display_timezone === val ? 'selected' : ''}>${label}</option>`).join('')}
                        </select>
                    </div>
                    </div>
                </details>

                <details class="settings-accordion">
                    <summary>Notification Filters</summary>
                    <div class="accordion-body">
                    <div class="settings-row">
                        <div>
                            <span class="settings-label">IB: Comments only</span>
                            <div style="font-size:11px;color:var(--text-muted);margin-top:2px">Only notify for new comments, not faves</div>
                        </div>
                        <label class="toggle-switch">
                            <input type="checkbox" id="pref-notif-comments-only" ${prefs.notification_comments_only ? 'checked' : ''}>
                            <span class="toggle-slider"></span>
                        </label>
                    </div>
                    <div class="settings-row">
                        <div>
                            <span class="settings-label">FA: Comments only</span>
                            <div style="font-size:11px;color:var(--text-muted);margin-top:2px">Only notify for new FA comments</div>
                        </div>
                        <label class="toggle-switch">
                            <input type="checkbox" id="pref-fa-notif-comments-only" ${prefs.fa_notification_comments_only ? 'checked' : ''}>
                            <span class="toggle-slider"></span>
                        </label>
                    </div>
                    <div class="settings-row">
                        <div>
                            <span class="settings-label">WS: Comments only</span>
                            <div style="font-size:11px;color:var(--text-muted);margin-top:2px">Only notify for Weasyl comment changes</div>
                        </div>
                        <label class="toggle-switch">
                            <input type="checkbox" id="pref-ws-notif-comments-only" ${prefs.ws_notification_comments_only ? 'checked' : ''}>
                            <span class="toggle-slider"></span>
                        </label>
                    </div>
                    <div class="settings-row">
                        <div>
                            <span class="settings-label">SF: Comments only</span>
                            <div style="font-size:11px;color:var(--text-muted);margin-top:2px">Only notify for SoFurry comment changes</div>
                        </div>
                        <label class="toggle-switch">
                            <input type="checkbox" id="pref-sf-notif-comments-only" ${prefs.sf_notification_comments_only ? 'checked' : ''}>
                            <span class="toggle-slider"></span>
                        </label>
                    </div>
                    <div class="settings-row">
                        <div>
                            <span class="settings-label">Min views delta</span>
                            <div style="font-size:11px;color:var(--text-muted);margin-top:2px">Minimum view increase to trigger notification</div>
                        </div>
                        <input type="number" class="search-input" id="pref-min-views-delta" value="${prefs.notification_min_views_delta || 0}" min="0" style="width:80px;text-align:center">
                    </div>
                    <div class="settings-row">
                        <div>
                            <span class="settings-label">Min faves delta</span>
                            <div style="font-size:11px;color:var(--text-muted);margin-top:2px">Minimum fave increase to trigger notification</div>
                        </div>
                        <input type="number" class="search-input" id="pref-min-faves-delta" value="${prefs.notification_min_faves_delta || 0}" min="0" style="width:80px;text-align:center">
                    </div>
                    </div>
                </details>

                <details class="settings-accordion">
                    <summary>Milestone Thresholds</summary>
                    <div class="accordion-body">
                    <p style="font-size:12px;color:var(--text-muted);margin-bottom:12px">Comma-separated numbers. Telegram will notify when a submission crosses any of these thresholds.</p>
                    <div class="settings-row" style="flex-direction:column;align-items:stretch;gap:8px">
                        <label style="font-size:13px;color:var(--text-muted)">View milestones</label>
                        <input type="text" id="pref-milestone-views" class="search-input" value="${(prefs.milestone_views || [100,250,500,1000,2500,5000,10000,25000,50000,100000]).join(', ')}" style="max-width:500px">
                    </div>
                    <div class="settings-row" style="flex-direction:column;align-items:stretch;gap:8px;margin-top:8px">
                        <label style="font-size:13px;color:var(--text-muted)">Fave milestones</label>
                        <input type="text" id="pref-milestone-faves" class="search-input" value="${(prefs.milestone_faves || [10,25,50,100,250,500,1000,2500,5000]).join(', ')}" style="max-width:500px">
                    </div>
                    <div class="settings-row" style="flex-direction:column;align-items:stretch;gap:8px;margin-top:8px">
                        <label style="font-size:13px;color:var(--text-muted)">Comment milestones</label>
                        <input type="text" id="pref-milestone-comments" class="search-input" value="${(prefs.milestone_comments || [10,25,50,100,250,500,1000]).join(', ')}" style="max-width:500px">
                    </div>
                    <p style="margin-top:12px;font-size:12px;color:var(--text-muted)">Saved with <strong>Save Settings</strong> at the top of the page.</p>
                    </div>
                </details>

                <!-- Publishing — relocated from its own tab into General (2.116.0).
                     All element IDs preserved so the Publishing handlers keep working. -->
                <details class="settings-accordion">
                    <summary>Publishing</summary>
                    <div class="accordion-body">
                    <div class="settings-row" style="flex-direction:column;align-items:stretch;gap:8px">
                        <label class="settings-toggle-row">
                            <span>Enable posting</span>
                            <input type="checkbox" id="posting-enabled-toggle" ${postingSettings.posting_enabled ? 'checked' : ''}>
                        </label>
                        <p style="font-size:12px;color:var(--text-muted);margin:2px 0 0">The master on/off for story, artwork and post publishing.</p>
                    </div>
                    <div class="settings-row" style="flex-direction:column;align-items:stretch;gap:8px;margin-top:12px">
                        <label style="font-size:13px;color:var(--text-muted)">Default Rating</label>
                        <select id="posting-default-rating" class="search-input" style="max-width:200px">
                            <option value="general" ${postingSettings.posting_default_rating === 'general' ? 'selected' : ''}>General</option>
                            <option value="mature" ${postingSettings.posting_default_rating === 'mature' ? 'selected' : ''}>Mature</option>
                            <option value="adult" ${postingSettings.posting_default_rating === 'adult' ? 'selected' : ''}>Adult</option>
                        </select>
                    </div>
                    <div class="settings-row" style="flex-direction:column;align-items:stretch;gap:8px;margin-top:12px">
                        <label style="font-size:13px;color:var(--text-muted)">Default Platforms (comma-separated: ib,fa,ws,sf,bsky)</label>
                        <input type="text" id="posting-default-platforms" class="search-input" value="${Utils.escapeHtml((postingSettings.posting_default_platforms || []).join(','))}" placeholder="ib,fa,sf" style="max-width:300px">
                    </div>
                    <div class="settings-row" style="flex-direction:column;align-items:stretch;gap:8px;margin-top:12px">
                        <label class="settings-toggle-row">
                            <span>🐾 "Posted via PawPoller" credit line</span>
                            <input type="checkbox" id="posting-attribution-toggle" ${postingSettings.pawpoller_attribution !== false ? 'checked' : ''}>
                        </label>
                        <p style="font-size:12px;color:var(--text-muted);margin:2px 0 0">Adds a small credit line to story and artwork descriptions when you publish. It's how other creators discover PawPoller. (Never added to short microblog posts.)</p>
                    </div>
                    <div class="settings-row" style="flex-direction:column;align-items:stretch;gap:8px;margin-top:14px;border-top:1px solid var(--card-border-inner);padding-top:12px">
                        <label class="settings-toggle-row">
                            <span>🖊️ Watermark artwork on export</span>
                            <input type="checkbox" id="art-wm-enabled">
                        </label>
                        <p style="font-size:12px;color:var(--text-muted);margin:2px 0 6px">Stamps a small text credit onto artwork images when you publish them (galleries + Bluesky). Doesn't touch your stored originals.</p>
                        <div style="display:flex;gap:10px;flex-wrap:wrap;align-items:center">
                            <input type="text" id="art-wm-text" class="search-input" placeholder="e.g. @yourhandle" style="max-width:220px">
                            <select id="art-wm-position" class="search-input" style="max-width:160px">
                                <option value="bottom-right">Bottom right</option>
                                <option value="bottom-left">Bottom left</option>
                                <option value="bottom-center">Bottom center</option>
                                <option value="top-right">Top right</option>
                                <option value="top-left">Top left</option>
                            </select>
                            <label style="font-size:12px;color:var(--text-muted)">Opacity
                                <input type="range" id="art-wm-opacity" min="0.1" max="1" step="0.1" value="0.5" style="vertical-align:middle"></label>
                            <button class="btn btn-sm btn-secondary" id="art-wm-save">Save</button>
                            <span id="art-wm-msg" class="muted" style="font-size:12px"></span>
                        </div>
                    </div>
                    </div>
                </details>

                <details class="settings-accordion">
                    <summary>Server Sync</summary>
                    <div class="accordion-body">
                    <p style="font-size:13px;color:var(--text-muted);margin-bottom:12px">
                        Configure these to enable the "Sync to Server" button on the Upload page.
                        The desktop app pushes your local story archive to the remote server.
                    </p>
                    <div class="settings-row" style="flex-direction:column;align-items:stretch;gap:8px">
                        <label style="font-size:13px;color:var(--text-muted)">Remote Server URL</label>
                        <input type="text" id="posting-server-url" class="search-input" value="${Utils.escapeHtml(postingSettings.posting_server_url || '')}" placeholder="http://34.xx.xx.xx:8420" style="max-width:400px">
                    </div>
                    <div class="settings-row" style="flex-direction:column;align-items:stretch;gap:8px;margin-top:8px">
                        <label style="font-size:13px;color:var(--text-muted)">Remote Server API Key</label>
                        <input type="text" id="posting-server-api-key" class="search-input" value="${Utils.escapeHtml(postingSettings.posting_server_api_key || '')}" placeholder="pp_xxxx..." style="max-width:400px">
                    </div>
                    <div class="settings-row" style="flex-direction:column;align-items:stretch;gap:8px;margin-top:8px">
                        <label style="font-size:13px;color:var(--text-muted)">Local Archive Path (auto-detected if blank)</label>
                        <input type="text" id="posting-archive-path" class="search-input" value="${Utils.escapeHtml(postingSettings.posting_story_archive_path || '')}" placeholder="Auto-detect" style="max-width:500px">
                    </div>
                    </div>
                </details>

                <div style="margin-top:16px;display:flex;gap:12px">
                    <button class="btn btn-primary" id="save-posting-settings-btn">Save Publishing Settings</button>
                    <span id="posting-settings-status" style="font-size:13px;color:var(--text-muted);align-self:center"></span>
                </div>

                <details class="settings-accordion" style="border-color:#a44;background:rgba(180,60,60,0.04)">
                    <summary style="color:#c66">Danger zone <span class="summary-meta">— uninstall, factory reset</span></summary>
                    <div class="accordion-body">
                    <div style="font-size:12px;color:var(--text-muted);margin-bottom:12px">
                        Removes PawPoller from this machine. The detected install type and
                        paths are shown in the confirmation dialog so you can sanity-check
                        before anything is deleted.
                    </div>
                    <div style="margin-top:12px">
                        <button class="btn" id="uninstall-btn"
                                style="background:#a44;color:#fff;border-color:#a44">
                            Uninstall PawPoller…
                        </button>
                    </div>
                    </div>
                </details>

                </div><!-- /tab:general -->

                <!-- ═══ TAB: Data ═══ -->
                <div class="settings-tab-content" data-tab-content="data" ${_settingsTab !== 'data' ? 'style="display:none"' : ''}>

                <div class="settings-section">
                    <h3>Export Data</h3>
                    <div style="font-size:12px;color:var(--text-muted);margin-bottom:12px">Download submission data as CSV files for each platform.</div>
                    <div style="display:flex;gap:8px;flex-wrap:wrap">
                        <button class="btn btn-secondary" data-export="ib">Export IB</button>
                        <button class="btn btn-secondary" data-export="fa">Export FA</button>
                        <button class="btn btn-secondary" data-export="ws">Export WS</button>
                        <button class="btn btn-secondary" data-export="sf">Export SF</button>
                        <button class="btn btn-secondary" data-export="sqw">Export SqW</button>
                        <button class="btn btn-secondary" data-export="ao3">Export AO3</button>
                        <button class="btn btn-secondary" data-export="da">Export DA</button>
                        <button class="btn btn-secondary" data-export="wp">Export WP</button>
                        <button class="btn btn-secondary" data-export="ik">Export IK</button>
                        <button class="btn btn-secondary" data-export="bsky">Export BSKY</button>
                        <button class="btn btn-secondary" data-export="tw">Export TW</button>
                        <button class="btn btn-secondary" data-export="mast">Export MAST</button>
                        <button class="btn btn-secondary" data-export="tum">Export TUM</button>
                        <button class="btn btn-secondary" data-export="pix">Export PIX</button>
                        <button class="btn btn-secondary" data-export="thr">Export THR</button>
                        <button class="btn btn-secondary" data-export="ig">Export IG</button>
                        <button class="btn btn-secondary" data-export="e621">Export e621</button>
                    </div>
                </div>

                <div class="settings-section">
                    <h3>Backup &amp; Restore</h3>
                    <div style="font-size:12px;color:var(--text-muted);margin-bottom:10px">
                        Download <strong>everything</strong> — your database, settings, saved logins and uploaded
                        media — as one <code>.zip</code>, or restore from one. <span id="backup-size"></span>
                    </div>
                    <div class="settings-row">
                        <div>
                            <span class="settings-label">Download a full backup</span>
                            <div style="font-size:11px;color:var(--text-muted);margin-top:2px">One file with all your data. ⚠️ It contains your saved logins — keep it somewhere safe.</div>
                        </div>
                        <button class="btn btn-secondary" id="backup-download-btn">Download Backup</button>
                    </div>
                    <div class="settings-row" style="margin-top:8px">
                        <div>
                            <span class="settings-label">Restore from a backup</span>
                            <div style="font-size:11px;color:var(--text-muted);margin-top:2px">Replaces your current data with the backup's. A safety copy of your current data is kept, and you'll need to restart afterwards.</div>
                        </div>
                        <div style="display:flex;align-items:center;gap:8px">
                            <input type="file" id="restore-file-input" accept=".zip" style="font-size:12px">
                            <button class="btn btn-danger" id="backup-restore-btn" disabled>Restore</button>
                        </div>
                    </div>
                    <hr style="border:none;border-top:1px solid var(--card-border-inner);margin:14px 0">
                    <div class="settings-row">
                        <div>
                            <span class="settings-label"><label style="cursor:pointer"><input type="checkbox" id="autobk-enabled" style="margin-right:6px;vertical-align:middle">Automatic backups</label></span>
                            <div style="font-size:11px;color:var(--text-muted);margin-top:2px">
                                Write a backup on a schedule so you never forget. Stored in
                                <code id="autobk-dir">…</code>; the oldest are pruned automatically. <span id="autobk-last"></span>
                            </div>
                        </div>
                        <div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap;justify-content:flex-end">
                            <label style="font-size:12px;color:var(--text-muted)">every <input type="number" id="autobk-interval" min="1" max="720" value="24" style="width:56px"> h</label>
                            <label style="font-size:12px;color:var(--text-muted)">keep <input type="number" id="autobk-keep" min="1" max="100" value="7" style="width:48px"></label>
                            <button class="btn btn-sm btn-secondary" id="autobk-save">Save</button>
                            <button class="btn btn-sm btn-outline" id="autobk-now">Back up now</button>
                        </div>
                    </div>
                    <span id="autobk-msg" style="font-size:12px;margin-top:4px;display:block"></span>
                    <span id="backup-msg" style="font-size:13px;margin-top:8px;display:block"></span>
                </div>

                <div class="settings-section">
                    <h3>Discord announcements</h3>
                    <div style="font-size:12px;color:var(--text-muted);margin-bottom:10px">
                        Post an announcement to a Discord channel whenever you publish. Paste a channel
                        <strong>webhook URL</strong> (Discord → Channel → Edit → Integrations → Webhooks → New Webhook → Copy URL).
                        Adult pieces announce as a link without the image preview.
                    </div>
                    <div class="settings-row">
                        <div style="flex:1">
                            <span class="settings-label">Webhook URL</span>
                            <div style="font-size:11px;color:var(--text-muted);margin-top:2px" id="discord-hint"></div>
                        </div>
                        <input type="password" id="discord-webhook" placeholder="https://discord.com/api/webhooks/…" autocomplete="off" style="flex:2;min-width:220px;font-size:12px">
                    </div>
                    <div class="settings-row" style="margin-top:8px">
                        <div>
                            <span class="settings-label"><label style="cursor:pointer"><input type="checkbox" id="discord-auto" style="margin-right:6px;vertical-align:middle">Announce automatically when I publish</label></span>
                            <div style="font-size:11px;color:var(--text-muted);margin-top:2px">Fires once per publish (posts + artwork), interactive or scheduled.</div>
                        </div>
                        <div style="display:flex;gap:8px">
                            <button class="btn btn-sm btn-secondary" id="discord-save">Save</button>
                            <button class="btn btn-sm btn-outline" id="discord-test">Send test</button>
                        </div>
                    </div>
                    <span id="discord-msg" style="font-size:12px;margin-top:6px;display:block"></span>
                </div>

                <div class="settings-section">
                    <h3>Settings Sync</h3>
                    <div style="font-size:12px;color:var(--text-muted);margin-bottom:12px">
                        Sync credentials and settings between desktop and server.
                        Requires a configured API key.
                    </div>
                    <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap">
                        <button class="btn btn-secondary" id="sync-pull-btn">Pull from server</button>
                        <button class="btn btn-secondary" id="sync-push-btn">Push to server</button>
                        <button class="btn btn-secondary" id="sync-status-btn">Check status</button>
                    </div>
                    <div id="sync-result" style="font-size:12px;margin-top:8px"></div>
                </div>

                <div class="settings-section">
                    <h3>Credential Security</h3>
                    <div style="font-size:12px;color:var(--text-muted);margin-bottom:8px">
                        Passwords and tokens are always stored in an encrypted vault —
                        plaintext credential storage no longer exists.
                    </div>
                    <div id="vault-result" style="font-size:12px">Checking key store…</div>
                </div>

                </div><!-- /tab:data -->

                <!-- ═══ TAB: Logs ═══ -->
                <div class="settings-tab-content" data-tab-content="logs" ${_settingsTab !== 'logs' ? 'style="display:none"' : ''}>

                <div class="settings-section">
                    <h3>Application Logs</h3>
                    <div style="display:flex;gap:8px;align-items:center;margin-bottom:12px;flex-wrap:wrap">
                        <select id="log-file-select" class="filter-select" style="max-width:160px">
                            <option value="server">server.log</option>
                            <option value="polling">polling.log</option>
                            <option value="app">app.log</option>
                        </select>
                        <select id="log-lines-select" class="filter-select" style="max-width:130px">
                            <option value="100">Last 100 lines</option>
                            <option value="200" selected>Last 200 lines</option>
                            <option value="500">Last 500 lines</option>
                            <option value="1000">Last 1000 lines</option>
                        </select>
                        <button class="btn btn-secondary" id="log-refresh-btn" style="padding:4px 12px;font-size:12px">Refresh</button>
                        <button class="btn btn-secondary" id="log-copy-btn" style="padding:4px 12px;font-size:12px" title="Copy visible log lines to clipboard">Copy</button>
                        <label style="display:flex;align-items:center;gap:4px;font-size:12px;color:var(--text-muted);margin-left:auto">
                            <input type="checkbox" id="log-auto-scroll" checked> Auto-scroll
                        </label>
                    </div>
                    <div style="font-size:11px;color:var(--text-muted);margin-bottom:8px" id="log-info"></div>
                    <pre id="log-output" style="background:var(--bg-primary);border:1px solid var(--border);border-radius:var(--radius);padding:12px;font-size:11px;line-height:1.5;max-height:500px;overflow:auto;white-space:pre-wrap;word-break:break-all;color:var(--text-secondary);font-family:'Cascadia Code','Fira Code','Consolas',monospace;user-select:text;-webkit-user-select:text;cursor:text"></pre>
                </div>

                </div><!-- /tab:logs -->

                <!-- ═══ TAB: About ═══ -->
                <div class="settings-tab-content" data-tab-content="about" ${_settingsTab !== 'about' ? 'style="display:none"' : ''}>

                ${updateInfo.available ? `
                <div class="settings-section" style="border-color:var(--success)">
                    <h3>Update Available</h3>
                    <div class="settings-row">
                        <div>
                            <span class="settings-label">Current: ${Utils.escapeHtml(updateInfo.current)} &rarr; Latest: ${Utils.escapeHtml(updateInfo.latest)}</span>
                            ${_isServer ? '<div style="font-size:11px;color:var(--text-muted);margin-top:4px">This is a server install — update by running <code>pawupdate</code> (or <code>git pull &amp;&amp; docker compose up -d --build</code>) on the host.</div>' : ''}
                        </div>
                        ${_isServer ? '' : '<button class="btn btn-primary" id="apply-update-btn">Update Now</button>'}
                    </div>
                </div>` : `
                <div class="settings-section">
                    <h3>Version</h3>
                    <div class="settings-row">
                        <span class="settings-label">Version ${Utils.escapeHtml(updateInfo.current)}</span>
                        <div style="display:flex;align-items:center;gap:10px">
                            <span class="settings-value" style="color:var(--success)" id="update-status-text">Up to date</span>
                            <button class="btn btn-secondary" id="check-update-btn" style="padding:4px 12px;font-size:12px">Check for Updates</button>
                        </div>
                    </div>
                </div>`}

                <div class="settings-section">
                    <h3>Website</h3>
                    <div class="settings-row">
                        <div>
                            <span class="settings-label">Marketing site &amp; project home</span>
                            <div style="font-size:11px;color:var(--text-muted);margin-top:4px">Features, screenshots and download links.</div>
                        </div>
                        <a class="btn btn-secondary" href="https://pawpoller.pages.dev" target="_blank" rel="noopener noreferrer" style="padding:4px 12px;font-size:12px">pawpoller.pages.dev &nearr;</a>
                    </div>
                </div>

                </div><!-- /tab:about -->

                <!-- ═══ TAB: Security ═══ -->
                <div class="settings-tab-content" data-tab-content="security" ${_settingsTab !== 'security' ? 'style="display:none"' : ''}>

                <details class="settings-accordion" open>
                    <summary>Change Password</summary>
                    <div class="accordion-body">
                    ${!App._dashboardAuthRequired ? `<div style="margin-bottom:12px;padding:10px 12px;border-radius:8px;background:rgba(255,196,0,0.12);border:1px solid rgba(255,196,0,0.35);font-size:13px">No dashboard password is set — the dashboard is open to anyone who can reach it. Recommended if you host on a server. <a href="#/dashboard-setup" style="font-weight:600">Set up a password &rarr;</a></div>` : ''}
                    <div class="settings-row" style="flex-direction:column;align-items:stretch;gap:8px">
                        <label style="font-size:13px;color:var(--text-muted)">Current Password</label>
                        <input type="password" id="sec-current-pw" class="search-input" placeholder="Current password" style="max-width:300px" autocomplete="current-password">
                    </div>
                    <div class="settings-row" style="flex-direction:column;align-items:stretch;gap:8px;margin-top:8px">
                        <label style="font-size:13px;color:var(--text-muted)">New Password</label>
                        <input type="password" id="sec-new-pw" class="search-input" placeholder="Minimum 8 characters" style="max-width:300px" autocomplete="new-password">
                    </div>
                    <div class="settings-row" style="flex-direction:column;align-items:stretch;gap:8px;margin-top:8px">
                        <label style="font-size:13px;color:var(--text-muted)">Confirm New Password</label>
                        <input type="password" id="sec-confirm-pw" class="search-input" placeholder="Confirm password" style="max-width:300px" autocomplete="new-password">
                    </div>
                    <div style="margin-top:12px;display:flex;align-items:center;gap:12px">
                        <button class="btn btn-primary" id="sec-change-pw-btn">Update Password</button>
                        <span id="sec-pw-msg" style="font-size:13px"></span>
                    </div>
                    </div>
                </details>

                <details class="settings-accordion">
                    <summary>Two-Factor Authentication <span id="sec-totp-badge" class="summary-meta"></span></summary>
                    <div class="accordion-body" id="sec-totp-body">
                        <p style="color:var(--text-muted);font-size:13px">Loading 2FA status...</p>
                    </div>
                </details>

                <details class="settings-accordion">
                    <summary>API Keys</summary>
                    <div class="accordion-body" id="sec-apikeys-body">
                        <p style="color:var(--text-muted);font-size:13px">Loading API keys...</p>
                    </div>
                </details>

                <details class="settings-accordion">
                    <summary>Cloudflare Turnstile</summary>
                    <div class="accordion-body">
                    <p style="color:var(--text-muted);font-size:13px;margin-bottom:12px">Add bot protection to the login page. Get keys from the <a href="https://dash.cloudflare.com" target="_blank" rel="noopener" style="color:var(--accent)">Cloudflare dashboard</a> &rarr; Turnstile.</p>
                    <div class="settings-row" style="flex-direction:column;align-items:stretch;gap:8px">
                        <label style="font-size:13px;color:var(--text-muted)">Site Key</label>
                        <input type="text" id="sec-ts-sitekey" class="search-input" placeholder="0x..." style="max-width:400px">
                    </div>
                    <div class="settings-row" style="flex-direction:column;align-items:stretch;gap:8px;margin-top:8px">
                        <label style="font-size:13px;color:var(--text-muted)">Secret Key</label>
                        <input type="password" id="sec-ts-secret" class="search-input" placeholder="0x..." style="max-width:400px">
                    </div>
                    <div style="margin-top:12px;display:flex;align-items:center;gap:12px">
                        <button class="btn btn-primary" id="sec-ts-save-btn">Save Turnstile Config</button>
                        <span id="sec-ts-msg" style="font-size:13px"></span>
                    </div>
                    </div>
                </details>

                </div><!-- /tab:security -->

                <!-- ═══ TAB: Telegram ═══ -->
                <div class="settings-tab-content" data-tab-content="telegram" ${_settingsTab !== 'telegram' ? 'style="display:none"' : ''}>

                <div class="settings-section">
                    <h3>Telegram Notifications</h3>
                    ${telegram.connected ? `
                    <div class="settings-row">
                        <div>
                            <span class="settings-label">Status</span>
                        </div>
                        <span class="telegram-status connected">Connected</span>
                    </div>
                    <div class="settings-row">
                        <div>
                            <span class="settings-label">Send Telegram notifications</span>
                            <div style="font-size:11px;color:var(--text-muted);margin-top:2px">Receive fave/comment alerts via Telegram</div>
                        </div>
                        <label class="toggle-switch">
                            <input type="checkbox" id="pref-telegram" ${telegram.enabled ? 'checked' : ''}>
                            <span class="toggle-slider"></span>
                        </label>
                    </div>
                    <div style="margin-top:12px;display:flex;align-items:center;gap:8px">
                        <button class="btn btn-secondary" id="telegram-test-btn">Test</button>
                        <button class="btn btn-danger" id="telegram-disconnect-btn">Disconnect</button>
                        <span id="telegram-msg" style="font-size:13px"></span>
                    </div>

                    <h3 style="margin-top:24px">Notification Features</h3>
                    <div class="settings-row">
                        <div><span class="settings-label">Poll summaries</span><div style="font-size:11px;color:var(--text-muted);margin-top:2px">Send a message after each poll cycle</div></div>
                        <label class="toggle-switch"><input type="checkbox" id="tg-feat-summaries" ${tgFeatures.poll_summaries ? 'checked' : ''}><span class="toggle-slider"></span></label>
                    </div>
                    <div class="settings-row">
                        <div><span class="settings-label">Error alerts</span><div style="font-size:11px;color:var(--text-muted);margin-top:2px">Send a message when a poll fails</div></div>
                        <label class="toggle-switch"><input type="checkbox" id="tg-feat-errors" ${tgFeatures.error_alerts ? 'checked' : ''}><span class="toggle-slider"></span></label>
                    </div>
                    <div class="settings-row">
                        <div><span class="settings-label">Milestones</span><div style="font-size:11px;color:var(--text-muted);margin-top:2px">Alert when submissions hit view/fave milestones</div></div>
                        <label class="toggle-switch"><input type="checkbox" id="tg-feat-milestones" ${tgFeatures.milestones ? 'checked' : ''}><span class="toggle-slider"></span></label>
                    </div>
                    <div class="settings-row">
                        <div><span class="settings-label">Periodic digest</span><div style="font-size:11px;color:var(--text-muted);margin-top:2px">Cross-platform summary sent on a timer</div></div>
                        <label class="toggle-switch"><input type="checkbox" id="tg-feat-digest" ${tgFeatures.digest ? 'checked' : ''}><span class="toggle-slider"></span></label>
                    </div>
                    <div class="settings-row">
                        <div><span class="settings-label">Digest interval</span><div style="font-size:11px;color:var(--text-muted);margin-top:2px">Hours between digest reports (1–168)</div></div>
                        <div style="display:flex;align-items:center;gap:8px">
                            <input type="number" id="tg-digest-interval" class="search-input" style="width:80px" min="1" max="168" value="${tgFeatures.digest_interval_hours}">
                            <span style="font-size:13px;color:var(--text-muted)">hours</span>
                        </div>
                    </div>
                    ` : `
                    <p class="telegram-instructions">Paste your bot token (from <a href="https://t.me/BotFather" target="_blank" style="color:var(--accent)">@BotFather</a>), send <code>/start</code> to your bot on Telegram, then click Connect.</p>
                    <div style="margin-top:12px;display:flex;gap:8px;align-items:center;flex-wrap:wrap">
                        <input type="text" id="telegram-token" class="search-input" placeholder="123456:ABC-DEF..." style="flex:1;min-width:200px">
                        <button class="btn btn-primary" id="telegram-connect-btn">Connect</button>
                    </div>
                    <div id="telegram-msg" style="font-size:13px;margin-top:8px"></div>
                    `}
                </div>

                <div class="settings-section">
                    <h3>Channel posting</h3>
                    <p style="font-size:13px;color:var(--text-muted);margin-bottom:12px;max-width:64ch">
                        Broadcast your <strong>Posts</strong> to a Telegram channel (text + images). Create a channel,
                        add your bot as an <strong>admin</strong> with post rights, then put the channel's
                        <code>@username</code> below. Uses your notification bot${tgChannel.has_own_token ? '' : ' by default'} —
                        or set a separate posting bot token.
                    </p>
                    <div class="settings-row" style="flex-direction:column;align-items:stretch;gap:6px">
                        <label style="font-size:13px;color:var(--text-muted)">Channel</label>
                        <input type="text" id="tgchan-channel" class="search-input" value="${Utils.escapeHtml(tgChannel.channel || '')}" placeholder="@publicchannel or -100…" style="max-width:300px">
                        <p style="color:var(--text-muted);font-size:12px;margin:6px 0 0">
                            A <strong>public</strong> channel uses its <code>@username</code> — the one in its public link, not its title.
                            A <strong>private</strong> channel has no username at all: it needs its numeric <code>-100…</code> id, which Telegram never shows you.
                            Press <strong>Find my channel</strong> and PawPoller will fetch it.
                        </p>
                    </div>
                    <div class="settings-row" style="flex-direction:column;align-items:stretch;gap:6px;margin-top:8px">
                        <label style="font-size:13px;color:var(--text-muted)">Posting bot token ${tgChannel.has_own_token ? '(saved — leave blank to keep)' : '(optional — blank reuses your notification bot)'}</label>
                        <input type="password" id="tgchan-token" class="search-input" placeholder="${tgChannel.has_own_token ? '********' : '123456:ABC-DEF…'}" style="max-width:300px" autocomplete="new-password">
                    </div>
                    <div class="settings-row" style="flex-direction:column;align-items:stretch;gap:6px;margin-top:14px">
                        <label style="font-size:13px;color:var(--text-muted)">Channel defaults for published work</label>
                        <p style="color:var(--text-muted);font-size:12px;margin:0 0 4px">
                            Applied to every artwork and story announcement sent here. Any single piece can override
                            them in its <code>art.json</code> under <code>categories.tg</code>.
                        </p>
                        <label class="tgopt"><input type="checkbox" id="tgchan-protect" ${tgChannel.protect ? 'checked' : ''}>
                            <span><strong>Stop forwarding &amp; saving</strong> — Telegram's own anti-repost control. Viewers can't forward the post or save the image.</span></label>
                        <label class="tgopt"><input type="checkbox" id="tgchan-document" ${tgChannel.document ? 'checked' : ''}>
                            <span><strong>Send at full quality</strong> — as a file rather than a photo. Telegram re-compresses photos; this keeps the original pixels, but shows as an attachment instead of inline.</span></label>
                        <label class="tgopt"><input type="checkbox" id="tgchan-silent" ${tgChannel.silent ? 'checked' : ''}>
                            <span><strong>Post silently</strong> — no notification ping. Useful for bulk catch-up posts.</span></label>
                        <label class="tgopt"><input type="checkbox" id="tgchan-notags" ${tgChannel.no_tags ? 'checked' : ''}>
                            <span><strong>No hashtags</strong> — send the description only.</span></label>
                    </div>
                    <div style="margin-top:14px;display:flex;align-items:center;gap:8px;flex-wrap:wrap">
                        <button class="btn btn-primary" id="tgchan-save">Save</button>
                        <button class="btn btn-secondary" id="tgchan-test">Save &amp; send test</button>
                        <button class="btn btn-outline" id="tgchan-detect" title="Post any message in your channel first, then press this">&#128269; Find my channel</button>
                        <span id="tgchan-msg" style="font-size:13px"></span>
                    </div>
                </div>

                </div><!-- /tab:telegram -->

                <!-- ═══ TAB: Weekly digest ═══ -->
                <div class="settings-tab-content" data-tab-content="digest" ${_settingsTab !== 'digest' ? 'style="display:none"' : ''}>
                <div class="settings-section">
                    <h3>Weekly digest email</h3>
                    <p style="font-size:13px;color:var(--text-muted);margin-bottom:14px;max-width:64ch">
                        A plain, templated recap of your week — views/faves/comments gained, growth by platform,
                        top gainers, new watchers and a few pieces worth resurfacing. Built entirely from your own
                        poll data: <strong>no AI, nothing shared</strong>. Delivered to your inbox via your own SMTP.
                    </p>
                    <div class="settings-row">
                        <div>
                            <span class="settings-label">Send the weekly digest</span>
                            <div style="font-size:11px;color:var(--text-muted);margin-top:2px">${digest.last_sent_at ? 'Last sent ' + Utils.escapeHtml(new Date(digest.last_sent_at).toLocaleString()) : 'Not sent yet'}</div>
                        </div>
                        <label class="toggle-switch">
                            <input type="checkbox" id="digest-enabled" ${digest.enabled ? 'checked' : ''}>
                            <span class="toggle-slider"></span>
                        </label>
                    </div>
                    <div class="settings-row" style="flex-direction:column;align-items:stretch;gap:6px;margin-top:8px">
                        <label style="font-size:13px;color:var(--text-muted)">Send to (one or more emails, comma-separated)</label>
                        <input type="text" id="digest-recipients" class="search-input" value="${Utils.escapeHtml(digest.recipients_raw || '')}" placeholder="you@example.com" style="max-width:420px">
                    </div>
                    <div class="settings-row" style="align-items:center;gap:8px;margin-top:8px">
                        <label style="font-size:13px;color:var(--text-muted)">Every</label>
                        <input type="number" id="digest-interval" class="search-input" style="width:70px" min="1" max="90" value="${digest.interval_days || 7}">
                        <span style="font-size:13px;color:var(--text-muted)">days</span>
                    </div>

                    <h3 style="margin-top:24px">Mail server (SMTP)</h3>
                    <p style="font-size:12px;color:var(--text-muted);margin-bottom:10px;max-width:64ch">
                        For Gmail, use <code>smtp.gmail.com</code> port <code>587</code>, your address as the username,
                        and a <a href="https://myaccount.google.com/apppasswords" target="_blank" style="color:var(--accent)">Google App Password</a> (not your normal password).
                    </p>
                    <div class="settings-row" style="flex-direction:column;align-items:stretch;gap:6px">
                        <label style="font-size:13px;color:var(--text-muted)">SMTP host</label>
                        <input type="text" id="digest-smtp-host" class="search-input" value="${Utils.escapeHtml(digest.smtp_host || 'smtp.gmail.com')}" style="max-width:300px">
                    </div>
                    <div class="settings-row" style="align-items:center;gap:8px;margin-top:8px">
                        <label style="font-size:13px;color:var(--text-muted)">Port</label>
                        <input type="number" id="digest-smtp-port" class="search-input" style="width:90px" value="${digest.smtp_port || 587}">
                        <label class="toggle-switch" style="margin-left:12px"><input type="checkbox" id="digest-smtp-tls" ${digest.smtp_use_tls !== false ? 'checked' : ''}><span class="toggle-slider"></span></label>
                        <span style="font-size:13px;color:var(--text-muted)">Use STARTTLS</span>
                    </div>
                    <div class="settings-row" style="flex-direction:column;align-items:stretch;gap:6px;margin-top:8px">
                        <label style="font-size:13px;color:var(--text-muted)">Username</label>
                        <input type="text" id="digest-smtp-user" class="search-input" value="${Utils.escapeHtml(digest.smtp_username || '')}" placeholder="you@gmail.com" style="max-width:300px">
                    </div>
                    <div class="settings-row" style="flex-direction:column;align-items:stretch;gap:6px;margin-top:8px">
                        <label style="font-size:13px;color:var(--text-muted)">Password / app password ${digest.has_password ? '(saved — leave blank to keep)' : ''}</label>
                        <input type="password" id="digest-smtp-pass" class="search-input" placeholder="${digest.has_password ? '********' : 'app password'}" style="max-width:300px" autocomplete="new-password">
                    </div>
                    <div class="settings-row" style="flex-direction:column;align-items:stretch;gap:6px;margin-top:8px">
                        <label style="font-size:13px;color:var(--text-muted)">From address (optional — defaults to username)</label>
                        <input type="text" id="digest-smtp-from" class="search-input" value="${Utils.escapeHtml(digest.smtp_from || '')}" placeholder="you@gmail.com" style="max-width:300px">
                    </div>

                    <div style="margin-top:16px;display:flex;align-items:center;gap:8px;flex-wrap:wrap">
                        <button class="btn btn-primary" id="digest-save-btn">Save</button>
                        <button class="btn btn-secondary" id="digest-preview-btn">Preview</button>
                        <button class="btn btn-secondary" id="digest-test-btn">Send test now</button>
                        <span id="digest-msg" style="font-size:13px"></span>
                    </div>
                </div>
                </div><!-- /tab:digest -->

                <!-- ═══ TAB: Platforms ═══ -->
                <div class="settings-tab-content" data-tab-content="platforms" ${_settingsTab !== 'platforms' ? 'style="display:none"' : ''}>

                ${setupStatus.setup_complete ? '' : `
                <div class="setup-return-banner">
                    <span>You're part-way through first-time setup. Connect what you need here, then head back.</span>
                    <a class="btn btn-sm btn-primary" href="#/setup">&#8592; Back to setup</a>
                </div>`}

                <details class="settings-accordion" open>
                    <summary><span class="status-dot" id="session-health-dot"></span>Session health <span class="summary-meta">— cookie / token validity</span></summary>
                    <div class="accordion-body">
                    <p style="color:var(--text-muted);font-size:13px;margin-bottom:12px">
                        PawPoller checks each credential platform's session on startup and every ~6 hours.
                        <span style="color:#7CFFB0">Green</span> = alive,
                        <span style="color:#FF8A8A">red</span> = expired (re-enter credentials below),
                        <span style="color:#FFD66B">amber</span> = couldn't verify (often a temporary network blip).
                        Force a re-check any time:
                    </p>
                    <div style="display:flex;align-items:center;gap:10px;flex-wrap:wrap;margin-bottom:12px">
                        <button class="btn btn-primary" id="session-check-btn">Check sessions now</button>
                        <span id="session-check-msg" style="font-size:13px;color:var(--text-muted)"></span>
                    </div>
                    <div id="session-status-list" class="session-status-list"></div>
                    </div>
                </details>

                <details class="settings-accordion" data-platform="ib">
                    <summary><span class="status-dot ${creds.has_password ? 'connected' : 'disconnected'}"></span>Inkbunny${creds.username ? ` <span class="summary-meta">— ${Utils.escapeHtml(creds.username)}</span>` : ''}</summary>
                    <div class="accordion-body">
                    <p style="color:var(--text-muted);font-size:13px;margin-bottom:12px">
                        Inkbunny's API needs username + password to mint a session ID — web cookies aren't usable for auth, so this is a direct credential form rather than a browser-login flow.${_browserLoginAvailable ? ' Use "Verify in Browser" if you want to confirm your password works against the IB website before saving.' : ''}
                    </p>
                    <div class="settings-row" style="flex-direction:column;align-items:stretch;gap:8px">
                        <label style="font-size:13px;color:var(--text-muted)">Username</label>
                        <input type="text" id="cred-username" class="search-input" value="${Utils.escapeHtml(creds.username || '')}" placeholder="Inkbunny username" style="max-width:300px">
                    </div>
                    <div class="settings-row" style="flex-direction:column;align-items:stretch;gap:8px;margin-top:8px">
                        <label style="font-size:13px;color:var(--text-muted)">Password ${creds.has_password ? '(saved — leave blank to keep)' : ''}</label>
                        <input type="password" id="cred-password" class="search-input" placeholder="${creds.has_password ? '********' : 'Inkbunny password'}" style="max-width:300px">
                    </div>
                    <div style="margin-top:12px;display:flex;align-items:center;gap:8px;flex-wrap:wrap">
                        <button class="btn btn-primary" id="save-creds-btn">Save Credentials</button>
                        ${_browserLoginAvailable ? '<button class="btn btn-secondary" id="ib-browser-login-btn" title="Open inkbunny.net in a popup to confirm credentials work">Verify in Browser</button>' : ''}
                        ${creds.has_password ? '<button class="btn btn-danger" id="settings-logout-btn">Sign Out</button>' : ''}
                        <span id="creds-msg" style="font-size:13px"></span>
                    </div>
                    </div>
                </details>

                <details class="settings-accordion" data-platform="fa">
                    <summary><span class="status-dot ${faAuth.has_cookies ? 'connected' : 'disconnected'}"></span>FurAffinity${faAuth.has_cookies ? ` <span class="summary-meta">— ${Utils.escapeHtml(faAuth.username || '')}</span>` : ''}</summary>
                    <div class="accordion-body">
                    ${faAuth.has_cookies ? `
                    <div class="settings-row">
                        <div>
                            <span class="settings-label">Status</span>
                        </div>
                        <span class="telegram-status connected">Connected as ${Utils.escapeHtml(faAuth.username || '')}</span>
                    </div>
                    <div class="settings-row" style="margin-top:8px">
                        <div>
                            <span class="settings-label">FA desktop notifications</span>
                            <div style="font-size:11px;color:var(--text-muted);margin-top:2px">Toast + Telegram alerts for FA comments</div>
                        </div>
                        <label class="toggle-switch">
                            <input type="checkbox" id="pref-fa-notifications" ${prefs.fa_notifications_enabled ? 'checked' : ''}>
                            <span class="toggle-slider"></span>
                        </label>
                    </div>
                    <div class="settings-row" style="margin-top:8px">
                        <div>
                            <span class="settings-label">FA watcher notifications</span>
                            <div style="font-size:11px;color:var(--text-muted);margin-top:2px">Toast + Telegram alerts for new FA watchers</div>
                        </div>
                        <label class="toggle-switch">
                            <input type="checkbox" id="pref-fa-watcher-notif" ${prefs.fa_watcher_notifications_enabled !== false ? 'checked' : ''}>
                            <span class="toggle-slider"></span>
                        </label>
                    </div>
                    <div style="margin-top:12px;display:flex;align-items:center;gap:8px;flex-wrap:wrap">
                        <button class="btn btn-primary" id="fa-poll-btn">FA Poll Now</button>
                        <button class="btn btn-secondary" id="fa-resync-btn">FA Full Resync</button>
                        <button class="btn btn-danger" id="fa-disconnect-btn">Disconnect</button>
                        <span id="fa-msg" style="font-size:13px"></span>
                    </div>
                    ` : `
                    ${_browserLoginAvailable ? `
                    <p style="color:var(--text-muted);font-size:13px;margin-bottom:12px">Log in to FurAffinity in the popup window. Your cookies will be captured automatically.</p>
                    <div style="display:flex;flex-direction:column;gap:8px;max-width:400px">
                        <input type="text" id="fa-browser-username" class="search-input" placeholder="FA username">
                    </div>
                    <div style="margin-top:12px;display:flex;align-items:center;gap:8px;flex-wrap:wrap">
                        <button class="btn btn-primary" id="fa-browser-login-btn">Login via Browser</button>
                        <button class="btn btn-outline" id="fa-manual-toggle" style="font-size:12px">Enter cookies manually</button>
                        <span id="fa-msg" style="font-size:13px"></span>
                    </div>
                    <div id="fa-manual-section" style="display:none;margin-top:16px;border-top:1px solid var(--border);padding-top:12px">
                        <p style="color:var(--text-muted);font-size:12px;margin-bottom:8px">Manual entry: open FA in your browser, find your <code style="background:var(--bg-tertiary);padding:2px 4px;border-radius:3px">a</code> and <code style="background:var(--bg-tertiary);padding:2px 4px;border-radius:3px">b</code> cookie values.</p>
                        <div style="display:flex;flex-direction:column;gap:8px;max-width:400px">
                            <input type="text" id="fa-username" class="search-input" placeholder="FA username">
                            <input type="text" id="fa-cookie-a" class="search-input" placeholder="Cookie a value">
                            <input type="text" id="fa-cookie-b" class="search-input" placeholder="Cookie b value">
                        </div>
                        <div style="margin-top:8px"><button class="btn btn-primary" id="fa-connect-btn">Connect</button></div>
                    </div>
                    ` : `
                    <p style="color:var(--text-muted);font-size:13px;margin-bottom:12px">Connect your FurAffinity account using browser cookies. <a href="https://www.furaffinity.net/login/" target="_blank" style="color:var(--accent)">Open FA login page</a>, log in, then find your <code style="background:var(--bg-tertiary);padding:2px 4px;border-radius:3px">a</code> and <code style="background:var(--bg-tertiary);padding:2px 4px;border-radius:3px">b</code> cookie values in DevTools (F12 > Application > Cookies).</p>
                    <div style="display:flex;flex-direction:column;gap:8px;max-width:400px">
                        <input type="text" id="fa-username" class="search-input" placeholder="FA username">
                        <input type="text" id="fa-cookie-a" class="search-input" placeholder="Cookie a value">
                        <input type="text" id="fa-cookie-b" class="search-input" placeholder="Cookie b value">
                    </div>
                    <div style="margin-top:12px;display:flex;align-items:center;gap:8px">
                        <button class="btn btn-primary" id="fa-connect-btn">Connect</button>
                        <span id="fa-msg" style="font-size:13px"></span>
                    </div>
                    `}
                    `}
                    </div>
                </details>

                <details class="settings-accordion" data-platform="ws">
                    <summary><span class="status-dot ${wsAuth.has_key ? 'connected' : 'disconnected'}"></span>Weasyl${wsAuth.has_key ? ` <span class="summary-meta">— ${Utils.escapeHtml(wsAuth.username || '')}</span>` : ''}</summary>
                    <div class="accordion-body">
                    ${wsAuth.has_key ? `
                    <div class="settings-row">
                        <div>
                            <span class="settings-label">Status</span>
                        </div>
                        <span class="telegram-status connected">Connected as ${Utils.escapeHtml(wsAuth.username || '')}</span>
                    </div>
                    <div class="settings-row" style="margin-top:8px">
                        <div>
                            <span class="settings-label">WS desktop notifications</span>
                            <div style="font-size:11px;color:var(--text-muted);margin-top:2px">Toast + Telegram alerts for WS activity</div>
                        </div>
                        <label class="toggle-switch">
                            <input type="checkbox" id="pref-ws-notifications" ${prefs.ws_notifications_enabled ? 'checked' : ''}>
                            <span class="toggle-slider"></span>
                        </label>
                    </div>
                    <div style="margin-top:12px;display:flex;align-items:center;gap:8px;flex-wrap:wrap">
                        <button class="btn btn-primary" id="ws-poll-btn">WS Poll Now</button>
                        <button class="btn btn-secondary" id="ws-resync-btn">WS Full Resync</button>
                        <button class="btn btn-danger" id="ws-disconnect-btn">Disconnect</button>
                        <span id="ws-msg" style="font-size:13px"></span>
                    </div>
                    ` : `
                    <p style="color:var(--text-muted);font-size:13px;margin-bottom:12px">Connect your Weasyl account using an API key. Get your key from <a href="https://www.weasyl.com/control/apikeys" target="_blank" style="color:var(--accent)">Weasyl API Keys</a>.</p>
                    <div style="display:flex;gap:8px;align-items:center;max-width:400px">
                        <input type="text" id="ws-api-key" class="search-input" placeholder="Weasyl API key" style="flex:1">
                        <button class="btn btn-primary" id="ws-connect-btn">Connect</button>
                    </div>
                    <div id="ws-msg" style="font-size:13px;margin-top:8px"></div>
                    `}
                    </div>
                </details>

                <details class="settings-accordion" data-platform="sf">
                    <summary><span class="status-dot ${sfAuth.has_credentials ? 'connected' : 'disconnected'}"></span>SoFurry${sfAuth.has_credentials ? ` <span class="summary-meta">— ${Utils.escapeHtml(sfAuth.username || '')}</span>` : ''}</summary>
                    <div class="accordion-body">
                    ${sfAuth.has_credentials ? `
                    <div class="settings-row">
                        <div>
                            <span class="settings-label">Status</span>
                        </div>
                        <span class="telegram-status connected">Connected as ${Utils.escapeHtml(sfAuth.username || '')}</span>
                    </div>
                    <div class="settings-row" style="margin-top:8px">
                        <div>
                            <span class="settings-label">SF desktop notifications</span>
                            <div style="font-size:11px;color:var(--text-muted);margin-top:2px">Toast + Telegram alerts for SF activity</div>
                        </div>
                        <label class="toggle-switch">
                            <input type="checkbox" id="pref-sf-notifications" ${prefs.sf_notifications_enabled ? 'checked' : ''}>
                            <span class="toggle-slider"></span>
                        </label>
                    </div>
                    <div style="margin-top:12px;display:flex;align-items:center;gap:8px;flex-wrap:wrap">
                        <button class="btn btn-primary" id="sf-poll-btn">SF Poll Now</button>
                        <button class="btn btn-secondary" id="sf-resync-btn">SF Full Resync</button>
                        <button class="btn btn-danger" id="sf-disconnect-btn">Disconnect</button>
                        <span id="sf-msg" style="font-size:13px"></span>
                    </div>
                    ` : `
                    <p style="color:var(--text-muted);font-size:13px;margin-bottom:12px">SoFurry now has an official API, so PawPoller no longer needs (or stores) your password. Create a <strong>Personal Access Token</strong> and paste it below — your profile name is read from the token automatically.</p>
                    <p style="font-size:13px;margin-bottom:12px"><a href="${Utils.escapeHtml(sfAuth.token_url || 'https://sofurry.com/settings/pat-create')}?name=PawPoller&description=Posting%20and%20analytics%20via%20PawPoller" target="_blank" rel="noopener noreferrer">Create a token on SoFurry &rarr;</a> <span style="color:var(--text-muted)">(Settings &rarr; Developer &rarr; New token)</span></p>
                    <div style="display:flex;flex-direction:column;gap:8px;max-width:400px">
                        <input type="password" id="sf-api-token" class="search-input" placeholder="Paste your SoFurry Personal Access Token" autocomplete="off">
                    </div>
                    <div style="margin-top:12px;display:flex;align-items:center;gap:8px">
                        <button class="btn btn-primary" id="sf-connect-btn">Connect</button>
                        <span id="sf-msg" style="font-size:13px"></span>
                    </div>
                    `}
                    </div>
                </details>

                <details class="settings-accordion" data-platform="sqw">
                    <summary><span class="status-dot ${sqwAuth.has_credentials ? 'connected' : 'disconnected'}"></span>SquidgeWorld${sqwAuth.has_credentials ? ` <span class="summary-meta">— ${Utils.escapeHtml(sqwAuth.username || '')}</span>` : ''}</summary>
                    <div class="accordion-body">
                    ${sqwAuth.has_credentials ? `
                    <div class="settings-row">
                        <div>
                            <span class="settings-label">Status</span>
                        </div>
                        <span class="telegram-status connected">Connected — tracking ${Utils.escapeHtml(sqwAuth.username || '')}</span>
                    </div>
                    <div class="settings-row" style="margin-top:8px">
                        <div>
                            <span class="settings-label">SqW desktop notifications</span>
                            <div style="font-size:11px;color:var(--text-muted);margin-top:2px">Toast + Telegram alerts for SqW activity</div>
                        </div>
                        <label class="toggle-switch">
                            <input type="checkbox" id="pref-sqw-notifications" ${prefs.sqw_notifications_enabled ? 'checked' : ''}>
                            <span class="toggle-slider"></span>
                        </label>
                    </div>
                    <div style="margin-top:12px;display:flex;align-items:center;gap:8px;flex-wrap:wrap">
                        <button class="btn btn-primary" id="sqw-poll-btn">SqW Poll Now</button>
                        <button class="btn btn-secondary" id="sqw-resync-btn">SqW Full Resync</button>
                        <button class="btn btn-danger" id="sqw-disconnect-btn">Disconnect</button>
                        <span id="sqw-msg" style="font-size:13px"></span>
                    </div>
                    ` : `
                    <p style="color:var(--text-muted);font-size:13px;margin-bottom:12px">Connect to SquidgeWorld with a login account and specify the user to track.</p>
                    <div style="display:flex;flex-direction:column;gap:8px;max-width:400px">
                        <input type="text" id="sqw-username" class="search-input" placeholder="Login username (e.g. PawPoller)">
                        <input type="password" id="sqw-password" class="search-input" placeholder="Login password">
                        <input type="text" id="sqw-target-user" class="search-input" placeholder="Target user to track (e.g. KnaughtyKat)">
                    </div>
                    <div style="margin-top:12px;display:flex;align-items:center;gap:8px">
                        <button class="btn btn-primary" id="sqw-connect-btn">Connect</button>
                        <span id="sqw-msg" style="font-size:13px"></span>
                    </div>
                    `}
                    </div>
                </details>

                <details class="settings-accordion" data-platform="ao3">
                    <summary><span class="status-dot ${ao3Auth.has_credentials ? 'connected' : 'disconnected'}"></span>AO3${ao3Auth.has_credentials ? ` <span class="summary-meta">— ${Utils.escapeHtml(ao3Auth.username || '')}</span>` : ''}</summary>
                    <div class="accordion-body">
                    ${ao3Auth.has_credentials ? `
                    <div class="settings-row">
                        <div>
                            <span class="settings-label">Status</span>
                        </div>
                        <span class="telegram-status connected">Connected — tracking ${Utils.escapeHtml(ao3Auth.username || '')}</span>
                    </div>
                    <div class="settings-row" style="margin-top:8px">
                        <div>
                            <span class="settings-label">AO3 desktop notifications</span>
                            <div style="font-size:11px;color:var(--text-muted);margin-top:2px">Toast + Telegram alerts for AO3 activity</div>
                        </div>
                        <label class="toggle-switch">
                            <input type="checkbox" id="pref-ao3-notifications" ${prefs.ao3_notifications_enabled ? 'checked' : ''}>
                            <span class="toggle-slider"></span>
                        </label>
                    </div>
                    <div style="margin-top:12px;display:flex;align-items:center;gap:8px;flex-wrap:wrap">
                        <button class="btn btn-primary" id="ao3-poll-btn">AO3 Poll Now</button>
                        <button class="btn btn-secondary" id="ao3-resync-btn">AO3 Full Resync</button>
                        <button class="btn btn-danger" id="ao3-disconnect-btn">Disconnect</button>
                        <span id="ao3-msg" style="font-size:13px"></span>
                    </div>
                    ` : `
                    <p style="color:var(--text-muted);font-size:13px;margin-bottom:12px">Connect to AO3 with a login account and specify the user to track.</p>
                    <div style="display:flex;flex-direction:column;gap:8px;max-width:400px">
                        <input type="text" id="ao3-username" class="search-input" placeholder="Login username (optional if using cookie)">
                        <input type="password" id="ao3-password" class="search-input" placeholder="Login password (optional if using cookie)">
                        <input type="text" id="ao3-target-user" class="search-input" placeholder="Target user to track">
                        <details style="margin-top:6px">
                            <summary style="cursor:pointer;font-size:12px;color:var(--text-muted)">Advanced: paste session cookie instead</summary>
                            <div style="margin-top:6px">
                                <input type="password" id="ao3-session-cookie" class="search-input" placeholder="_otwarchive_session cookie value">
                                <p style="font-size:11px;color:var(--text-muted);margin-top:4px;line-height:1.4">From your logged-in browser: DevTools → Application → Cookies → archiveofourown.org → copy the <code>_otwarchive_session</code> value. Bypasses AO3's per-IP login throttle (recommended when running on a server).</p>
                            </div>
                        </details>
                    </div>
                    <div style="margin-top:12px;display:flex;align-items:center;gap:8px">
                        <button class="btn btn-primary" id="ao3-connect-btn">Connect</button>
                        <span id="ao3-msg" style="font-size:13px"></span>
                    </div>
                    `}
                    </div>
                </details>

                <details class="settings-accordion" data-platform="da">
                    <summary><span class="status-dot ${daAuth.has_credentials ? 'connected' : 'disconnected'}"></span>DeviantArt${daAuth.has_credentials ? ` <span class="summary-meta">— ${Utils.escapeHtml(daAuth.username || '')}</span>` : ''}</summary>
                    <div class="accordion-body">
                    ${daAuth.has_credentials ? `
                    <div class="settings-row">
                        <div>
                            <span class="settings-label">Status</span>
                        </div>
                        <span class="telegram-status connected">Connected — tracking ${Utils.escapeHtml(daAuth.username || '')}</span>
                    </div>
                    <div class="settings-row" style="margin-top:8px">
                        <div>
                            <span class="settings-label">DA desktop notifications</span>
                            <div style="font-size:11px;color:var(--text-muted);margin-top:2px">Toast + Telegram alerts for DA activity</div>
                        </div>
                        <label class="toggle-switch">
                            <input type="checkbox" id="pref-da-notifications" ${prefs.da_notifications_enabled ? 'checked' : ''}>
                            <span class="toggle-slider"></span>
                        </label>
                    </div>
                    <div style="margin-top:12px;display:flex;align-items:center;gap:8px;flex-wrap:wrap">
                        <button class="btn btn-primary" id="da-poll-btn">DA Poll Now</button>
                        <button class="btn btn-secondary" id="da-resync-btn">DA Full Resync</button>
                        <button class="btn btn-danger" id="da-disconnect-btn">Disconnect</button>
                        <span id="da-msg" style="font-size:13px"></span>
                    </div>
                    <div class="settings-row" style="margin-top:16px;border-top:1px solid var(--border);padding-top:14px">
                        <div>
                            <span class="settings-label">Posting to DeviantArt</span>
                            <div style="font-size:11px;color:var(--text-muted);margin-top:2px">
                                Separate from polling. Reading your stats only needs the app;
                                <em>posting as you</em> needs your approval in a browser, which is
                                the only way DeviantArt will issue the token for it.
                            </div>
                        </div>
                        <span id="da-posting-status" class="telegram-status disconnected">Checking...</span>
                    </div>
                    <div style="margin-top:10px;display:flex;align-items:center;gap:8px;flex-wrap:wrap">
                        <button class="btn btn-secondary" id="da-authorize-btn">Authorise posting</button>
                        <span id="da-authorize-msg" style="font-size:13px"></span>
                    </div>
                    <div id="da-redirect-hint" style="display:none;margin-top:10px;padding:10px;
                         background:var(--bg-input);border-radius:6px;font-size:12px;color:var(--text-muted)">
                        DeviantArt only redirects back to an address registered on the app. Add this one at
                        <a href="https://www.deviantart.com/developers/apps" target="_blank" style="color:var(--accent)">Applications</a>
                        &rarr; Edit &rarr; OAuth2 Redirect URI Whitelist, then try again:
                        <div style="margin-top:6px"><code id="da-redirect-uri" style="user-select:all"></code></div>
                    </div>
                    ` : `
                    <p style="color:var(--text-muted);font-size:13px;margin-bottom:12px">Connect DeviantArt with the official API — no browser cookie needed. <a href="https://www.deviantart.com/developers/apps" target="_blank" style="color:var(--accent)">Register a DA app</a> (Client type: <strong>Confidential</strong>), then paste its client_id and client_secret below plus the username to track.</p>
                    <div style="display:flex;flex-direction:column;gap:8px;max-width:400px">
                        <input type="text" id="da-client-id" class="search-input" placeholder="client_id (e.g. 12345)">
                        <input type="password" id="da-client-secret" class="search-input" placeholder="client_secret">
                        <input type="text" id="da-target-user" class="search-input" placeholder="DeviantArt username to track">
                    </div>
                    <div style="margin-top:12px;display:flex;align-items:center;gap:8px">
                        <button class="btn btn-primary" id="da-connect-btn">Connect</button>
                        <span id="da-msg" style="font-size:13px"></span>
                    </div>
                    `}
                    </div>
                </details>

                <details class="settings-accordion" data-platform="wp">
                    <summary><span class="status-dot ${wpAuth.has_credentials ? 'connected' : 'disconnected'}"></span>Wattpad${wpAuth.has_credentials ? ` <span class="summary-meta">— ${Utils.escapeHtml(wpAuth.username || '')}</span>` : ''}</summary>
                    <div class="accordion-body">
                    ${wpAuth.has_credentials ? `
                    <div class="settings-row">
                        <div>
                            <span class="settings-label">Status</span>
                        </div>
                        <span class="telegram-status connected">Connected — tracking ${Utils.escapeHtml(wpAuth.username || '')}</span>
                    </div>
                    <div class="settings-row" style="margin-top:8px">
                        <div>
                            <span class="settings-label">WP desktop notifications</span>
                            <div style="font-size:11px;color:var(--text-muted);margin-top:2px">Toast + Telegram alerts for Wattpad activity</div>
                        </div>
                        <label class="toggle-switch">
                            <input type="checkbox" id="pref-wp-notifications" ${prefs.wp_notifications_enabled ? 'checked' : ''}>
                            <span class="toggle-slider"></span>
                        </label>
                    </div>
                    <div style="margin-top:12px;display:flex;align-items:center;gap:8px;flex-wrap:wrap">
                        <button class="btn btn-primary" id="wp-poll-btn">WP Poll Now</button>
                        <button class="btn btn-secondary" id="wp-resync-btn">WP Full Resync</button>
                        <button class="btn btn-danger" id="wp-disconnect-btn">Disconnect</button>
                        <span id="wp-msg" style="font-size:13px"></span>
                    </div>
                    ` : `
                    <p style="color:var(--text-muted);font-size:13px;margin-bottom:12px">Connect to Wattpad by entering the username to track. No auth required — just enter the username.</p>
                    <div style="display:flex;flex-direction:column;gap:8px;max-width:400px">
                        <input type="text" id="wp-target-user" class="search-input" placeholder="Wattpad username to track">
                    </div>
                    <div style="margin-top:12px;display:flex;align-items:center;gap:8px">
                        <button class="btn btn-primary" id="wp-connect-btn">Connect</button>
                        <span id="wp-msg" style="font-size:13px"></span>
                    </div>
                    `}
                    </div>
                </details>

                <details class="settings-accordion" data-platform="ik">
                    <summary><span class="status-dot ${ikAuth.has_credentials ? 'connected' : 'disconnected'}"></span>Itaku${ikAuth.has_credentials ? ` <span class="summary-meta">— ${Utils.escapeHtml(ikAuth.username || '')}</span>` : ''}</summary>
                    <div class="accordion-body">
                    ${ikAuth.has_credentials ? `
                    <div class="settings-row">
                        <div>
                            <span class="settings-label">Status</span>
                        </div>
                        <span class="telegram-status connected">Connected — tracking ${Utils.escapeHtml(ikAuth.username || '')}</span>
                    </div>
                    <div class="settings-row" style="margin-top:8px">
                        <div>
                            <span class="settings-label">IK desktop notifications</span>
                            <div style="font-size:11px;color:var(--text-muted);margin-top:2px">Toast + Telegram alerts for Itaku activity</div>
                        </div>
                        <label class="toggle-switch">
                            <input type="checkbox" id="pref-ik-notifications" ${prefs.ik_notifications_enabled ? 'checked' : ''}>
                            <span class="toggle-slider"></span>
                        </label>
                    </div>
                    <div class="settings-row" style="margin-top:8px">
                        <div>
                            <span class="settings-label">Posting</span>
                            <div style="font-size:11px;color:var(--text-muted);margin-top:2px">
                                Posting to Itaku needs an auth token (tracking doesn't).
                                <strong>It is not the <code>sessionid</code> cookie.</strong>
                                On itaku.ee: DevTools &rarr; Network &rarr; <strong>Fetch/XHR</strong>,
                                click any request to <code>itaku.ee/api/</code>, and copy the
                                <strong>Authorization</strong> request header &mdash; the part after
                                <code>Token&nbsp;</code>. The token is checked with Itaku before it is saved.
                            </div>
                        </div>
                        <span class="telegram-status ${ikAuth.has_auth_token ? 'connected' : 'disconnected'}">${ikAuth.has_auth_token ? 'Auth token set — posting enabled' : 'No token — tracking only'}</span>
                    </div>
                    <div style="margin-top:8px;display:flex;align-items:center;gap:8px;flex-wrap:wrap;max-width:520px">
                        <input type="password" id="ik-auth-token" class="search-input" style="flex:1;min-width:220px" autocomplete="off" placeholder="${ikAuth.has_auth_token ? 'Replace auth token' : 'Paste auth token to enable posting'}">
                        <button class="btn btn-secondary" id="ik-savetoken-btn">${ikAuth.has_auth_token ? 'Update token' : 'Save token'}</button>
                        <span id="ik-token-msg" style="font-size:13px"></span>
                    </div>
                    <div style="margin-top:12px;display:flex;align-items:center;gap:8px;flex-wrap:wrap">
                        <button class="btn btn-primary" id="ik-poll-btn">IK Poll Now</button>
                        <button class="btn btn-secondary" id="ik-resync-btn">IK Full Resync</button>
                        <button class="btn btn-danger" id="ik-disconnect-btn">Disconnect</button>
                        <span id="ik-msg" style="font-size:13px"></span>
                    </div>
                    ` : `
                    <p style="color:var(--text-muted);font-size:13px;margin-bottom:12px">Enter the username to <strong>track</strong> Itaku. To also <strong>post</strong> to Itaku, add your auth token (copied from your logged-in Itaku session) — tracking works without it.</p>
                    <div style="display:flex;flex-direction:column;gap:8px;max-width:400px">
                        <input type="text" id="ik-target-user" class="search-input" placeholder="Itaku username to track">
                        <input type="password" id="ik-auth-token" class="search-input" placeholder="Auth token (optional — required to post)" autocomplete="off">
                    </div>
                    <div style="margin-top:12px;display:flex;align-items:center;gap:8px">
                        <button class="btn btn-primary" id="ik-connect-btn">Connect</button>
                        <span id="ik-msg" style="font-size:13px"></span>
                    </div>
                    `}
                    </div>
                </details>

                <details class="settings-accordion" data-platform="bsky">
                    <summary><span class="status-dot ${bskyAuth.has_credentials ? 'connected' : 'disconnected'}"></span>Bluesky${bskyAuth.has_credentials ? ` <span class="summary-meta">— ${Utils.escapeHtml(bskyAuth.username || '')}</span>` : ''}</summary>
                    <div class="accordion-body">
                    ${bskyAuth.has_credentials ? `
                    <div class="settings-row">
                        <div>
                            <span class="settings-label">Status</span>
                        </div>
                        <span class="telegram-status connected">Connected — tracking ${Utils.escapeHtml(bskyAuth.username || '')}</span>
                    </div>
                    <div class="settings-row" style="margin-top:8px">
                        <div>
                            <span class="settings-label">BSKY desktop notifications</span>
                            <div style="font-size:11px;color:var(--text-muted);margin-top:2px">Toast + Telegram alerts for Bluesky activity</div>
                        </div>
                        <label class="toggle-switch">
                            <input type="checkbox" id="pref-bsky-notifications" ${prefs.bsky_notifications_enabled ? 'checked' : ''}>
                            <span class="toggle-slider"></span>
                        </label>
                    </div>
                    <div style="margin-top:12px;display:flex;align-items:center;gap:8px;flex-wrap:wrap">
                        <button class="btn btn-primary" id="bsky-poll-btn">BSKY Poll Now</button>
                        <button class="btn btn-secondary" id="bsky-resync-btn">BSKY Full Resync</button>
                        <button class="btn btn-danger" id="bsky-disconnect-btn">Disconnect</button>
                        <span id="bsky-msg" style="font-size:13px"></span>
                    </div>
                    ` : `
                    <p style="color:var(--text-muted);font-size:13px;margin-bottom:12px">Connect to Bluesky with your handle and app password. Create an app password at Settings > App Passwords on bsky.app.</p>
                    <div style="display:flex;flex-direction:column;gap:8px;max-width:400px">
                        <input type="text" id="bsky-identifier" class="search-input" placeholder="Handle (e.g. user.bsky.social)">
                        <input type="password" id="bsky-app-password" class="search-input" placeholder="App password (xxxx-xxxx-xxxx-xxxx)">
                    </div>
                    <div style="margin-top:12px;display:flex;align-items:center;gap:8px">
                        <button class="btn btn-primary" id="bsky-connect-btn">Connect</button>
                        <span id="bsky-msg" style="font-size:13px"></span>
                    </div>
                    `}
                    </div>
                </details>

                <details class="settings-accordion" data-platform="mast">
                    <summary><span class="status-dot ${mastAuth.has_credentials ? 'connected' : 'disconnected'}"></span>Mastodon${mastAuth.flavour && mastAuth.flavour !== 'Mastodon' ? ' <span class="summary-meta" style="color:var(--text-muted)">/ ' + Utils.escapeHtml(mastAuth.flavour) + '</span>' : ''}${mastAuth.has_credentials ? ` <span class="summary-meta">— ${Utils.escapeHtml(mastAuth.username || '')}</span>` : ''}</summary>
                    <div class="accordion-body">
                    ${mastAuth.has_credentials ? `
                    <div class="settings-row">
                        <div>
                            <span class="settings-label">Status</span>
                        </div>
                        <span class="telegram-status connected">Connected — tracking ${Utils.escapeHtml(mastAuth.username || '')}${mastAuth.flavour ? ' (' + Utils.escapeHtml(mastAuth.flavour) + ')' : ''}</span>
                    </div>
                    <div class="settings-row" style="margin-top:8px">
                        <div>
                            <span class="settings-label">MAST desktop notifications</span>
                            <div style="font-size:11px;color:var(--text-muted);margin-top:2px">Toast + Telegram alerts for Mastodon activity</div>
                        </div>
                        <label class="toggle-switch">
                            <input type="checkbox" id="pref-mast-notifications" ${prefs.mast_notifications_enabled ? 'checked' : ''}>
                            <span class="toggle-slider"></span>
                        </label>
                    </div>
                    <div style="margin-top:12px;display:flex;align-items:center;gap:8px;flex-wrap:wrap">
                        <button class="btn btn-primary" id="mast-poll-btn">MAST Poll Now</button>
                        <button class="btn btn-secondary" id="mast-resync-btn">MAST Full Resync</button>
                        <button class="btn btn-danger" id="mast-disconnect-btn">Disconnect</button>
                        <span id="mast-msg" style="font-size:13px"></span>
                    </div>
                    ` : `
                    <p style="color:var(--text-muted);font-size:13px;margin-bottom:12px">Connect to Mastodon with your instance URL and a personal access token. On your instance go to Settings &gt; Development &gt; New application, give it the <code>read</code> scope, and copy "Your access token".</p>
                    <p style="color:var(--text-muted);font-size:12px;margin-bottom:12px;padding:8px 10px;background:var(--bg-subtle,rgba(127,127,127,.08));border-radius:6px">Works with any <strong>Mastodon-compatible fediverse server</strong> — Pleroma, Akkoma, GoToSocial, Firefish, Pixelfed and more all speak the same API. Just enter that server's URL and a read token; PawPoller detects which software it is automatically.</p>
                    <div style="display:flex;flex-direction:column;gap:8px;max-width:400px">
                        <input type="text" id="mast-instance-url" class="search-input" placeholder="Instance URL (e.g. https://mastodon.social)">
                        <input type="password" id="mast-access-token" class="search-input" placeholder="Access token">
                    </div>
                    <div style="margin-top:12px;display:flex;align-items:center;gap:8px">
                        <button class="btn btn-primary" id="mast-connect-btn">Connect</button>
                        <span id="mast-msg" style="font-size:13px"></span>
                    </div>
                    `}
                    </div>
                </details>

                <details class="settings-accordion" data-platform="tum">
                    <summary><span class="status-dot ${tumAuth.has_credentials ? 'connected' : 'disconnected'}"></span>Tumblr${tumAuth.has_credentials ? ` <span class="summary-meta">— ${Utils.escapeHtml(tumAuth.username || '')}</span>` : ''}</summary>
                    <div class="accordion-body">
                    ${tumAuth.has_credentials ? `
                    <div class="settings-row">
                        <div>
                            <span class="settings-label">Status</span>
                        </div>
                        <span class="telegram-status connected">Connected — tracking ${Utils.escapeHtml(tumAuth.username || '')}</span>
                    </div>
                    <div class="settings-row" style="margin-top:8px">
                        <div>
                            <span class="settings-label">TUM desktop notifications</span>
                            <div style="font-size:11px;color:var(--text-muted);margin-top:2px">Toast + Telegram alerts for Tumblr activity</div>
                        </div>
                        <label class="toggle-switch">
                            <input type="checkbox" id="pref-tum-notifications" ${prefs.tum_notifications_enabled ? 'checked' : ''}>
                            <span class="toggle-slider"></span>
                        </label>
                    </div>
                    <div style="margin-top:12px;display:flex;align-items:center;gap:8px;flex-wrap:wrap">
                        <button class="btn btn-primary" id="tum-poll-btn">TUM Poll Now</button>
                        <button class="btn btn-secondary" id="tum-resync-btn">TUM Full Resync</button>
                        <button class="btn btn-danger" id="tum-disconnect-btn">Disconnect</button>
                        <span id="tum-msg" style="font-size:13px"></span>
                    </div>
                    ` : `
                    <p style="color:var(--text-muted);font-size:13px;margin-bottom:12px">Connect to Tumblr with an app API key and a blog name. Register an app at <code>tumblr.com/oauth/apps</code>, copy the <strong>OAuth Consumer Key</strong>, and enter the blog you want to track.</p>
                    <div style="display:flex;flex-direction:column;gap:8px;max-width:400px">
                        <input type="text" id="tum-blog" class="search-input" placeholder="Blog (e.g. staff or staff.tumblr.com)">
                        <input type="password" id="tum-api-key" class="search-input" placeholder="OAuth Consumer Key">
                    </div>
                    <div style="margin-top:12px;display:flex;align-items:center;gap:8px">
                        <button class="btn btn-primary" id="tum-connect-btn">Connect</button>
                        <span id="tum-msg" style="font-size:13px"></span>
                    </div>
                    `}
                    </div>
                </details>

                <details class="settings-accordion" data-platform="pix">
                    <summary><span class="status-dot ${pixAuth.has_credentials ? 'connected' : 'disconnected'}"></span>Pixiv${pixAuth.has_credentials ? ` <span class="summary-meta">— ${Utils.escapeHtml(pixAuth.username || '')}</span>` : ''}</summary>
                    <div class="accordion-body">
                    ${pixAuth.has_credentials ? `
                    <div class="settings-row">
                        <div>
                            <span class="settings-label">Status</span>
                        </div>
                        <span class="telegram-status connected">Connected — tracking ${Utils.escapeHtml(pixAuth.username || '')}</span>
                    </div>
                    <div class="settings-row" style="margin-top:8px">
                        <div>
                            <span class="settings-label">PIX desktop notifications</span>
                            <div style="font-size:11px;color:var(--text-muted);margin-top:2px">Toast + Telegram alerts for Pixiv activity</div>
                        </div>
                        <label class="toggle-switch">
                            <input type="checkbox" id="pref-pix-notifications" ${prefs.pix_notifications_enabled ? 'checked' : ''}>
                            <span class="toggle-slider"></span>
                        </label>
                    </div>
                    <div style="margin-top:12px;display:flex;align-items:center;gap:8px;flex-wrap:wrap">
                        <button class="btn btn-primary" id="pix-poll-btn">PIX Poll Now</button>
                        <button class="btn btn-secondary" id="pix-resync-btn">PIX Full Resync</button>
                        <button class="btn btn-danger" id="pix-disconnect-btn">Disconnect</button>
                        <span id="pix-msg" style="font-size:13px"></span>
                    </div>
                    ` : `
                    <p style="color:var(--text-muted);font-size:13px;margin-bottom:12px">Connect to Pixiv with a one-time <strong>refresh token</strong>. Pixiv has no official API, so the token is obtained via a browser login (e.g. the <code>gppt</code> helper). Optionally set a target user ID to track someone else's public works (defaults to your own account).</p>
                    <div style="display:flex;flex-direction:column;gap:8px;max-width:400px">
                        <input type="password" id="pix-refresh-token" class="search-input" placeholder="Refresh token">
                        <input type="text" id="pix-user-id" class="search-input" placeholder="Target user ID (optional)">
                    </div>
                    <div style="margin-top:12px;display:flex;align-items:center;gap:8px">
                        <button class="btn btn-primary" id="pix-connect-btn">Connect</button>
                        <span id="pix-msg" style="font-size:13px"></span>
                    </div>
                    `}
                    </div>
                </details>

                <details class="settings-accordion" data-platform="thr">
                    <summary><span class="status-dot ${thrAuth.has_credentials ? 'connected' : 'disconnected'}"></span>Threads${thrAuth.has_credentials ? ` <span class="summary-meta">— ${Utils.escapeHtml(thrAuth.username || '')}</span>` : ''}</summary>
                    <div class="accordion-body">
                    ${thrAuth.has_credentials ? `
                    <div class="settings-row">
                        <div>
                            <span class="settings-label">Status</span>
                        </div>
                        <span class="telegram-status connected">Connected — tracking ${Utils.escapeHtml(thrAuth.username || '')}</span>
                    </div>
                    <div class="settings-row" style="margin-top:8px">
                        <div>
                            <span class="settings-label">THR desktop notifications</span>
                            <div style="font-size:11px;color:var(--text-muted);margin-top:2px">Toast + Telegram alerts for Threads activity</div>
                        </div>
                        <label class="toggle-switch">
                            <input type="checkbox" id="pref-thr-notifications" ${prefs.thr_notifications_enabled ? 'checked' : ''}>
                            <span class="toggle-slider"></span>
                        </label>
                    </div>
                    <div style="margin-top:12px;display:flex;align-items:center;gap:8px;flex-wrap:wrap">
                        <button class="btn btn-primary" id="thr-poll-btn">THR Poll Now</button>
                        <button class="btn btn-secondary" id="thr-resync-btn">THR Full Resync</button>
                        <button class="btn btn-danger" id="thr-disconnect-btn">Disconnect</button>
                        <span id="thr-msg" style="font-size:13px"></span>
                    </div>
                    ` : `
                    <p style="color:var(--text-muted);font-size:13px;margin-bottom:12px">Connect to Threads with a <strong>long-lived access token</strong> from a Meta app that has the <code>threads_basic</code> + <code>threads_manage_insights</code> scopes. <em>Note: Meta gates this behind app review and restricts adult content — it may not work for all accounts.</em> User ID is optional (defaults to your own account).</p>
                    <div style="display:flex;flex-direction:column;gap:8px;max-width:400px">
                        <input type="password" id="thr-access-token" class="search-input" placeholder="Long-lived access token">
                        <input type="text" id="thr-user-id" class="search-input" placeholder="User ID (optional)">
                    </div>
                    <div style="margin-top:12px;display:flex;align-items:center;gap:8px">
                        <button class="btn btn-primary" id="thr-connect-btn">Connect</button>
                        <span id="thr-msg" style="font-size:13px"></span>
                    </div>
                    `}
                    </div>
                </details>

                <details class="settings-accordion" data-platform="ig">
                    <summary><span class="status-dot ${igAuth.has_credentials ? 'connected' : 'disconnected'}"></span>Instagram${igAuth.has_credentials ? ` <span class="summary-meta">— ${Utils.escapeHtml(igAuth.username || '')}</span>` : ''}</summary>
                    <div class="accordion-body">
                    ${igAuth.has_credentials ? `
                    <div class="settings-row">
                        <div>
                            <span class="settings-label">Status</span>
                        </div>
                        <span class="telegram-status connected">Connected — tracking ${Utils.escapeHtml(igAuth.username || '')}</span>
                    </div>
                    <div class="settings-row" style="margin-top:8px">
                        <div>
                            <span class="settings-label">IG desktop notifications</span>
                            <div style="font-size:11px;color:var(--text-muted);margin-top:2px">Toast + Telegram alerts for Instagram activity</div>
                        </div>
                        <label class="toggle-switch">
                            <input type="checkbox" id="pref-ig-notifications" ${prefs.ig_notifications_enabled ? 'checked' : ''}>
                            <span class="toggle-slider"></span>
                        </label>
                    </div>
                    <div style="margin-top:12px;display:flex;align-items:center;gap:8px;flex-wrap:wrap">
                        <button class="btn btn-primary" id="ig-poll-btn">IG Poll Now</button>
                        <button class="btn btn-secondary" id="ig-resync-btn">IG Full Resync</button>
                        <button class="btn btn-danger" id="ig-disconnect-btn">Disconnect</button>
                        <span id="ig-msg" style="font-size:13px"></span>
                    </div>
                    ` : `
                    <p style="color:var(--text-muted);font-size:13px;margin-bottom:12px">Connect to Instagram with a <strong>long-lived Instagram user token</strong> from a Meta app that has the <code>instagram_business_basic</code> + <code>instagram_business_manage_insights</code> scopes, generated for a <strong>Business or Creator</strong> account. User ID is optional (defaults to your own account).</p>
                    <div style="display:flex;flex-direction:column;gap:8px;max-width:400px">
                        <input type="password" id="ig-access-token" class="search-input" placeholder="Long-lived access token">
                        <input type="text" id="ig-user-id" class="search-input" placeholder="User ID (optional)">
                    </div>
                    <div style="margin-top:12px;display:flex;align-items:center;gap:8px">
                        <button class="btn btn-primary" id="ig-connect-btn">Connect</button>
                        <span id="ig-msg" style="font-size:13px"></span>
                    </div>
                    `}
                    </div>
                </details>

                <details class="settings-accordion" data-platform="tw">
                    <summary><span class="status-dot ${twAuth.has_credentials ? 'connected' : 'disconnected'}"></span>X / Twitter${twAuth.has_credentials ? ` <span class="summary-meta">— ${Utils.escapeHtml(twAuth.username || '')}</span>` : ''}</summary>
                    <div class="accordion-body">
                    <div style="background:var(--bg-subtle);border:1px solid var(--border);border-radius:8px;padding:12px;margin-bottom:14px">
                        <div style="display:flex;justify-content:space-between;align-items:center;gap:8px;flex-wrap:wrap">
                            <span class="settings-label">Official X API <span style="color:var(--text-muted);font-weight:normal">(optional)</span></span>
                            <span class="tag" style="font-size:10px">poll backend: ${Utils.escapeHtml(twAuth.poll_backend || 'graphql')}</span>
                        </div>
                        <div style="font-size:11px;color:var(--text-muted);margin:6px 0 10px">
                            ToS-compliant, IP-agnostic polling via the official X API v2 — sidesteps the datacenter rate-limit the scrapers hit. Bring your own Bearer token from <a href="https://developer.x.com" target="_blank" style="color:var(--accent)">developer.x.com</a> (pay-per-use; owned reads ~$0.001 each, roughly $2–7/month at a normal cadence). One token covers all your X accounts. Unset = free gallery-dl/scrape. Polling only — posting still uses cookies.
                        </div>
                        ${twAuth.has_api_token ? `
                        <div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap">
                            <span class="telegram-status connected">API token set — official backend active</span>
                            <button class="btn btn-danger" id="tw-apitoken-remove-btn" style="font-size:12px">Remove token</button>
                            <span id="tw-apitoken-msg" style="font-size:13px"></span>
                        </div>
                        <div class="settings-row" style="margin-top:10px;border-top:1px solid var(--border);padding-top:10px">
                            <div>
                                <span class="settings-label">Throttle polling to save API costs</span>
                                <div style="font-size:11px;color:var(--text-muted);margin-top:2px">Poll a rotating 2 of your X accounts per scheduled cycle instead of all — fewer paid reads. Off = poll every account each cycle (the official API has no rate limit).</div>
                            </div>
                            <label class="toggle-switch">
                                <input type="checkbox" id="pref-tw-save-tokens" ${prefs.tw_roundrobin_save_tokens ? 'checked' : ''}>
                                <span class="toggle-slider"></span>
                            </label>
                        </div>
                        ` : `
                        <div style="display:flex;flex-direction:column;gap:6px;max-width:460px">
                            <input type="password" id="tw-api-bearer" class="search-input" placeholder="X API v2 Bearer token">
                            <input type="text" id="tw-api-target" class="search-input" placeholder="Username to track (without @)" value="${Utils.escapeHtml(twAuth.username || '')}">
                        </div>
                        <div style="margin-top:8px;display:flex;align-items:center;gap:8px">
                            <button class="btn btn-primary" id="tw-apitoken-save-btn" style="font-size:12px">Save API token</button>
                            <span id="tw-apitoken-msg" style="font-size:13px"></span>
                        </div>
                        `}
                    </div>
                    ${twAuth.has_credentials ? `
                    <div class="settings-row">
                        <div>
                            <span class="settings-label">Status</span>
                        </div>
                        <span class="telegram-status connected">Connected — tracking ${Utils.escapeHtml(twAuth.username || '')}</span>
                    </div>
                    <div class="settings-row" style="margin-top:8px">
                        <div>
                            <span class="settings-label">TW desktop notifications</span>
                            <div style="font-size:11px;color:var(--text-muted);margin-top:2px">Toast + Telegram alerts for X/Twitter activity</div>
                        </div>
                        <label class="toggle-switch">
                            <input type="checkbox" id="pref-tw-notifications" ${prefs.tw_notifications_enabled ? 'checked' : ''}>
                            <span class="toggle-slider"></span>
                        </label>
                    </div>
                    <div style="margin-top:12px;display:flex;align-items:center;gap:8px;flex-wrap:wrap">
                        <button class="btn btn-primary" id="tw-poll-btn">TW Poll Now</button>
                        <button class="btn btn-secondary" id="tw-resync-btn">TW Full Resync</button>
                        <button class="btn btn-danger" id="tw-disconnect-btn">Disconnect</button>
                        <span id="tw-msg" style="font-size:13px"></span>
                    </div>
                    ` : `
                    ${_browserLoginAvailable ? `
                    <p style="color:var(--text-muted);font-size:13px;margin-bottom:12px">Log in to X/Twitter in the popup window. Your auth cookies will be captured automatically.</p>
                    <div style="display:flex;flex-direction:column;gap:8px;max-width:400px">
                        <input type="text" id="tw-browser-username" class="search-input" placeholder="Username to track (without @)">
                    </div>
                    <div style="margin-top:12px;display:flex;align-items:center;gap:8px;flex-wrap:wrap">
                        <button class="btn btn-primary" id="tw-browser-login-btn">Login via Browser</button>
                        <button class="btn btn-outline" id="tw-manual-toggle" style="font-size:12px">Enter cookies manually</button>
                        <span id="tw-msg" style="font-size:13px"></span>
                    </div>
                    <div id="tw-manual-section" style="display:none;margin-top:16px;border-top:1px solid var(--border);padding-top:12px">
                        <p style="color:var(--text-muted);font-size:12px;margin-bottom:8px">Manual entry: open x.com, press F12, go to Application > Cookies, and copy the auth_token and ct0 values.</p>
                        <div style="display:flex;flex-direction:column;gap:8px;max-width:400px">
                            <input type="text" id="tw-auth-token" class="search-input" placeholder="auth_token cookie">
                            <input type="text" id="tw-ct0" class="search-input" placeholder="ct0 cookie">
                            <input type="text" id="tw-target-user" class="search-input" placeholder="Username to track (without @)">
                        </div>
                        <div style="margin-top:8px"><button class="btn btn-primary" id="tw-connect-btn">Connect</button></div>
                    </div>
                    ` : `
                    <p style="color:var(--text-muted);font-size:13px;margin-bottom:12px">Connect to X/Twitter using browser cookies. <a href="https://x.com/i/flow/login" target="_blank" style="color:var(--accent)">Open X login page</a>, log in, then press F12, go to Application > Cookies, and copy the auth_token and ct0 values.</p>
                    <div style="display:flex;flex-direction:column;gap:8px;max-width:400px">
                        <input type="text" id="tw-auth-token" class="search-input" placeholder="auth_token cookie">
                        <input type="text" id="tw-ct0" class="search-input" placeholder="ct0 cookie">
                        <input type="text" id="tw-target-user" class="search-input" placeholder="Username to track (without @)">
                    </div>
                    <div style="margin-top:12px;display:flex;align-items:center;gap:8px">
                        <button class="btn btn-primary" id="tw-connect-btn">Connect</button>
                        <span id="tw-msg" style="font-size:13px"></span>
                    </div>
                    `}
                    `}
                    </div>
                </details>

                <details class="settings-accordion" data-platform="e621">
                    <summary><span class="status-dot ${e621Auth.has_credentials ? 'connected' : 'disconnected'}"></span>e621${e621Auth.has_credentials ? ` <span class="summary-meta">— ${Utils.escapeHtml(e621Auth.username || '')}</span>` : ''}</summary>
                    <div class="accordion-body">
                    ${e621Auth.has_credentials ? `
                    <div class="settings-row">
                        <div>
                            <span class="settings-label">Status</span>
                        </div>
                        <span class="telegram-status connected">Connected — tracking ${Utils.escapeHtml(e621Auth.username || '')}</span>
                    </div>
                    <div class="settings-row" style="margin-top:8px">
                        <div>
                            <span class="settings-label">e621 desktop notifications</span>
                            <div style="font-size:11px;color:var(--text-muted);margin-top:2px">Toast + Telegram alerts for e621 activity</div>
                        </div>
                        <label class="toggle-switch">
                            <input type="checkbox" id="pref-e621-notifications" ${prefs.e621_notifications_enabled ? 'checked' : ''}>
                            <span class="toggle-slider"></span>
                        </label>
                    </div>
                    <div style="margin-top:12px;display:flex;align-items:center;gap:8px;flex-wrap:wrap">
                        <button class="btn btn-primary" id="e621-poll-btn">e621 Poll Now</button>
                        <button class="btn btn-secondary" id="e621-resync-btn">e621 Full Resync</button>
                        <button class="btn btn-danger" id="e621-disconnect-btn">Disconnect</button>
                        <span id="e621-msg" style="font-size:13px"></span>
                    </div>
                    ` : `
                    <p style="color:var(--text-muted);font-size:13px;margin-bottom:12px">Connect to e621 with your <strong>username + API key</strong>. Generate the key on e621 under <em>Account → Manage API Access</em> (this is the API key, <strong>not</strong> your password). Poll-only — tracks your own uploads' score, favorites and comments.</p>
                    <div style="display:flex;flex-direction:column;gap:8px;max-width:400px">
                        <input type="text" id="e621-username" class="search-input" placeholder="e621 username">
                        <input type="password" id="e621-api-key" class="search-input" placeholder="API key">
                    </div>
                    <div style="margin-top:12px;display:flex;align-items:center;gap:8px">
                        <button class="btn btn-primary" id="e621-connect-btn">Connect</button>
                        <span id="e621-msg" style="font-size:13px"></span>
                    </div>
                    `}
                    </div>
                </details>

                <details class="settings-accordion" data-platform="fn">
                    <summary><span class="status-dot ${fnAuth.has_credentials ? 'connected' : 'disconnected'}"></span>FurryNetwork${fnAuth.has_credentials ? ` <span class="summary-meta">— ${Utils.escapeHtml(fnAuth.username || '')}</span>` : ''}</summary>
                    <div class="accordion-body">
                    ${fnAuth.has_credentials ? `
                    <div class="settings-row">
                        <div><span class="settings-label">Status</span></div>
                        <span class="telegram-status connected">Connected — tracking ${Utils.escapeHtml(fnAuth.username || '')}</span>
                    </div>
                    <div class="settings-row" style="margin-top:8px">
                        <div>
                            <span class="settings-label">FurryNetwork desktop notifications</span>
                            <div style="font-size:11px;color:var(--text-muted);margin-top:2px">Toast + Telegram alerts for FurryNetwork activity</div>
                        </div>
                        <label class="toggle-switch">
                            <input type="checkbox" id="pref-fn-notifications" ${prefs.fn_notifications_enabled ? 'checked' : ''}>
                            <span class="toggle-slider"></span>
                        </label>
                    </div>
                    <div style="margin-top:12px;display:flex;align-items:center;gap:8px;flex-wrap:wrap">
                        <button class="btn btn-primary" id="fn-poll-btn">FurryNetwork Poll Now</button>
                        <button class="btn btn-secondary" id="fn-resync-btn">Full Resync</button>
                        <button class="btn btn-danger" id="fn-disconnect-btn">Disconnect</button>
                        <span id="fn-msg" style="font-size:13px"></span>
                    </div>
                    ` : `
                    <p style="color:var(--text-muted);font-size:13px;margin-bottom:12px">
                        Poll+post — tracks your submissions' views, favourites and comments across your characters.
                        <br><strong style="color:var(--warning,#d29922)">Email + password no longer works.</strong>
                        FurryNetwork put its login behind a reCAPTCHA check that no app can pass, so paste a
                        <strong>refresh token</strong> instead. It only has to be done once — PawPoller renews it from there.
                    </p>
                    <details style="margin-bottom:12px">
                        <summary style="cursor:pointer;font-size:13px;color:var(--accent)">How to get a refresh token</summary>
                        <ol style="font-size:12px;color:var(--text-muted);margin:8px 0 0 18px;line-height:1.7">
                            <li>Log in at <a href="https://furrynetwork.com" target="_blank" style="color:var(--accent)">furrynetwork.com</a> in your browser (the real page solves the reCAPTCHA).</li>
                            <li>Open DevTools (F12) &rarr; <strong>Application</strong> &rarr; Local Storage &rarr; furrynetwork.com.</li>
                            <li>Find <code>refresh_token</code> and copy its value. (Or: Network tab &rarr; the <code>oauth/token</code> response.)</li>
                        </ol>
                    </details>
                    <div style="display:flex;flex-direction:column;gap:8px;max-width:400px">
                        <input type="password" id="fn-refresh-token" class="search-input" placeholder="Refresh token (paste here)" autocomplete="off">
                        <div style="font-size:11px;color:var(--text-muted)">Optional — the email is only used as a label if the token doesn't carry a name:</div>
                        <input type="text" id="fn-username" class="search-input" placeholder="FurryNetwork email (optional)" autocomplete="username">
                        <input type="password" id="fn-password" class="search-input" placeholder="Password (kept for when FN drops the reCAPTCHA)" autocomplete="current-password">
                    </div>
                    <div style="margin-top:12px;display:flex;align-items:center;gap:8px">
                        <button class="btn btn-primary" id="fn-connect-btn">Connect</button>
                        <span id="fn-msg" style="font-size:13px"></span>
                    </div>
                    `}
                    </div>
                </details>

                <details class="settings-accordion" data-platform="fbr">
                    <summary><span class="status-dot ${fbrAuth.has_credentials ? 'connected' : 'disconnected'}"></span>Furbooru${fbrAuth.has_credentials ? ` <span class="summary-meta">— ${Utils.escapeHtml(fbrAuth.username || '')}</span>` : ''}</summary>
                    <div class="accordion-body">
                    ${fbrAuth.has_credentials ? `
                    <div class="settings-row">
                        <div><span class="settings-label">Status</span></div>
                        <span class="telegram-status connected">Connected — tracking ${Utils.escapeHtml(fbrAuth.username || '')}</span>
                    </div>
                    <div class="settings-row" style="margin-top:8px">
                        <div>
                            <span class="settings-label">Furbooru desktop notifications</span>
                            <div style="font-size:11px;color:var(--text-muted);margin-top:2px">Toast + Telegram alerts for Furbooru activity</div>
                        </div>
                        <label class="toggle-switch">
                            <input type="checkbox" id="pref-fbr-notifications" ${prefs.fbr_notifications_enabled ? 'checked' : ''}>
                            <span class="toggle-slider"></span>
                        </label>
                    </div>
                    <div style="margin-top:12px;display:flex;align-items:center;gap:8px;flex-wrap:wrap">
                        <button class="btn btn-primary" id="fbr-poll-btn">Furbooru Poll Now</button>
                        <button class="btn btn-secondary" id="fbr-resync-btn">Full Resync</button>
                        <button class="btn btn-danger" id="fbr-disconnect-btn">Disconnect</button>
                        <span id="fbr-msg" style="font-size:13px"></span>
                    </div>
                    ` : `
                    <p style="color:var(--text-muted);font-size:13px;margin-bottom:12px">Track your own <strong>Furbooru</strong> uploads' score, favourites and comments. Enter your Furbooru <strong>username</strong> — that's all that's required (Furbooru's read API is public). Optionally add an <strong>API key</strong> (Furbooru → <em>Account → Settings</em>) to raise the anonymous rate limit. Poll-only.</p>
                    <div style="display:flex;flex-direction:column;gap:8px;max-width:400px">
                        <input type="text" id="fbr-username" class="search-input" placeholder="Furbooru username" autocomplete="username">
                        <input type="password" id="fbr-api-key" class="search-input" placeholder="API key (optional)">
                    </div>
                    <div style="margin-top:12px;display:flex;align-items:center;gap:8px">
                        <button class="btn btn-primary" id="fbr-connect-btn">Connect</button>
                        <span id="fbr-msg" style="font-size:13px"></span>
                    </div>
                    `}
                    </div>
                </details>

                </div><!-- /tab:platforms -->

                <!-- ═══ TAB: Polling ═══ -->
                <div class="settings-tab-content" data-tab-content="polling" ${_settingsTab !== 'polling' ? 'style="display:none"' : ''}>

                <details class="settings-accordion" open>
                    <summary><span class="status-dot ${pollPausedState.polling_paused ? 'disconnected' : 'connected'}"></span>Polling Control <span class="summary-meta">— ${pollPausedState.polling_paused ? 'Paused' : 'Active'}</span></summary>
                    <div class="accordion-body">
                    <div class="settings-row">
                        <span class="settings-label">${pollPausedState.polling_paused ? 'Resume scheduled polling for all platforms' : 'Pause scheduled polling for all platforms'}</span>
                        <button class="btn ${pollPausedState.polling_paused ? 'btn-primary' : 'btn-danger'}" id="poll-pause-btn">
                            ${pollPausedState.polling_paused ? 'Resume Polling' : 'Pause Polling'}
                        </button>
                    </div>
                    <p style="font-size:12px;color:var(--text-muted);margin-top:8px;">
                        When paused, scheduled background polls are skipped. Manual "Poll Now" buttons still work.
                    </p>
                    </div>
                </details>

                <div id="polling-platforms-container">
                    <div style="text-align:center;padding:40px;color:var(--text-muted)">Loading polling data...</div>
                </div>

                <details class="settings-accordion">
                    <summary>CF Proxy Backup <span class="summary-meta">— retry through Cloudflare Worker only when a direct call fails</span></summary>
                    <div class="accordion-body">
                        <p style="font-size:12px;color:var(--text-muted);margin-bottom:12px;">
                            DeviantArt and SoFurry always use the configured CF Worker proxy — they need it from datacenter IPs. The platforms below run direct by default, with the proxy as a <em>fallback</em>: when toggled on, a poll/import/connect that hits a block-like failure (403, 429, "Retry later", Cloudflare challenge, persistent timeout) is automatically retried once through the Worker. Direct stays the happy path; the Worker quota only burns on actual failures. Requires <code>cf_worker_url</code> + <code>cf_worker_key</code> to be set.
                        </p>
                        <div id="cf-proxy-toggles">
                            <div style="text-align:center;padding:20px;color:var(--text-muted)">Loading…</div>
                        </div>
                    </div>
                </details>

                </div><!-- /tab:polling -->
            `;

            this._setContent(html);

            // ── Settings Tab Switching ───────────────────────────────
            const tabBar = document.getElementById('settings-tabs');
            if (tabBar) {
                tabBar.addEventListener('click', (e) => {
                    const btn = e.target.closest('.settings-tab');
                    if (!btn) return;
                    const tab = btn.dataset.stab;
                    // Update active tab button
                    tabBar.querySelectorAll('.settings-tab').forEach(t => t.classList.remove('active'));
                    btn.classList.add('active');
                    // Show/hide tab content panels
                    document.querySelectorAll('.settings-tab-content').forEach(panel => {
                        panel.style.display = panel.dataset.tabContent === tab ? '' : 'none';
                    });
                    // Update URL hash without re-rendering
                    const newHash = tab === 'general' ? '#/settings' : `#/settings/${tab}`;
                    history.replaceState(null, '', newHash);
                    // Auto-load lazy tabs when switching
                    if (tab === 'logs') this._loadLogs();
                    if (tab === 'polling') this._loadPollingTab();
                    if (tab === 'diagnostics' && window.Diagnostics) {
                        window.Diagnostics.mount(document.getElementById('diagnostics-mount'));
                    }
                });

                // Scroll-aware edge fades + keep the active tab in view, so it's
                // obvious on narrow screens that the strip scrolls to more tabs.
                // Listener is on the (per-render) tabBar element, so it's GC'd
                // with it — no accumulation across settings re-renders.
                const updateTabFade = () => {
                    const max = tabBar.scrollWidth - tabBar.clientWidth;
                    tabBar.classList.toggle('of-start', tabBar.scrollLeft > 4);
                    tabBar.classList.toggle('of-end', tabBar.scrollLeft < max - 4);
                };
                tabBar.addEventListener('scroll', updateTabFade, { passive: true });
                requestAnimationFrame(() => {
                    tabBar.querySelector('.settings-tab.active')
                        ?.scrollIntoView({ inline: 'center', block: 'nearest' });
                    updateTabFade();
                });
            }

            // ── Settings Search (2.103.0) ─────────────────────────────
            // Filters every settings tab at once. Typing reveals all panels
            // and hides the section/accordion units whose text doesn't match;
            // clearing restores the normal single-tab view.
            this._wireSettingsSearch(tabBar);

            // Load lazy tabs on initial render if active
            this._pollingTabLoaded = false;
            this._settingsSearchLoadedLazy = false; // re-arm search's eager-load (2.103.0)
            if (_settingsTab === 'logs') this._loadLogs();
            if (_settingsTab === 'polling') this._loadPollingTab();
            if (_settingsTab === 'diagnostics' && window.Diagnostics) {
                window.Diagnostics.mount(document.getElementById('diagnostics-mount'));
            }

            // Platforms pane: sort the platform accordions A→Z + give each a logo
            // and a centred title. Runs regardless of the active tab (the pane is
            // in the DOM either way) and is idempotent per render.
            this._enhancePlatformSettings();

            // ── Theme picker (Appearance tab) ─────────────────────────
            // Click any card to apply the theme. applyTheme() persists +
            // re-renders the current page, which redraws this picker with
            // the freshly-active card highlighted.
            const picker = document.getElementById('theme-picker');
            if (picker) {
                picker.addEventListener('click', (e) => {
                    const card = e.target.closest('.theme-card');
                    if (!card) return;
                    const themeId = card.dataset.themeId;
                    if (!themeId) return;
                    this.applyTheme(themeId);
                });
                picker.addEventListener('keydown', (e) => {
                    if (e.key !== 'Enter' && e.key !== ' ') return;
                    const card = e.target.closest('.theme-card');
                    if (!card) return;
                    e.preventDefault();
                    this.applyTheme(card.dataset.themeId);
                });
            }

            // ── Mobile-mode picker (Appearance tab) ───────────────────
            // Same shape as the theme picker. applyMobileMode() persists,
            // re-resolves data-mobile, and re-renders the page.
            const mmPicker = document.getElementById('mobile-mode-picker');
            if (mmPicker) {
                mmPicker.addEventListener('click', (e) => {
                    const card = e.target.closest('.mobile-mode-card');
                    if (!card) return;
                    this.applyMobileMode(card.dataset.mmId);
                });
                mmPicker.addEventListener('keydown', (e) => {
                    if (e.key !== 'Enter' && e.key !== ' ') return;
                    const card = e.target.closest('.mobile-mode-card');
                    if (!card) return;
                    e.preventDefault();
                    this.applyMobileMode(card.dataset.mmId);
                });
            }

            // ── Nav-mode picker (Appearance tab) — top bar vs side rail ──
            const navPicker = document.getElementById('nav-mode-picker');
            if (navPicker) {
                navPicker.addEventListener('click', (e) => {
                    const card = e.target.closest('.mobile-mode-card');
                    if (!card) return;
                    this.applyNavMode(card.dataset.navId);
                });
                navPicker.addEventListener('keydown', (e) => {
                    if (e.key !== 'Enter' && e.key !== ' ') return;
                    const card = e.target.closest('.mobile-mode-card');
                    if (!card) return;
                    e.preventDefault();
                    this.applyNavMode(card.dataset.navId);
                });
            }

            // ── Display-mode picker (Appearance tab) — Default vs Brut ──
            const modePicker = document.getElementById('display-mode-picker');
            if (modePicker) {
                modePicker.addEventListener('click', (e) => {
                    const card = e.target.closest('.mobile-mode-card');
                    if (!card) return;
                    this.applyMode(card.dataset.modeId);
                });
                modePicker.addEventListener('keydown', (e) => {
                    if (e.key !== 'Enter' && e.key !== ' ') return;
                    const card = e.target.closest('.mobile-mode-card');
                    if (!card) return;
                    e.preventDefault();
                    this.applyMode(card.dataset.modeId);
                });
            }

            // Log tab event handlers
            document.getElementById('log-refresh-btn')?.addEventListener('click', () => this._loadLogs());
            document.getElementById('log-file-select')?.addEventListener('change', () => this._loadLogs());
            document.getElementById('log-lines-select')?.addEventListener('change', () => this._loadLogs());
            document.getElementById('log-copy-btn')?.addEventListener('click', async (e) => {
                const btn = e.target;
                const out = document.getElementById('log-output');
                const text = out ? (out.textContent || '') : '';
                if (!text) { btn.textContent = 'Empty'; setTimeout(() => { btn.textContent = 'Copy'; }, 1200); return; }
                try {
                    await navigator.clipboard.writeText(text);
                    btn.textContent = 'Copied';
                } catch (_) {
                    // Fallback: select the <pre> contents so the user can Ctrl+C.
                    // pywebview WebView2 occasionally rejects clipboard writes
                    // when the window doesn't have focus from a real input.
                    try {
                        const range = document.createRange();
                        range.selectNodeContents(out);
                        const sel = window.getSelection();
                        sel.removeAllRanges();
                        sel.addRange(range);
                        btn.textContent = 'Selected — Ctrl+C';
                    } catch (_) {
                        btn.textContent = 'Copy failed';
                    }
                }
                setTimeout(() => { btn.textContent = 'Copy'; }, 1500);
            });

            // ── Settings Event Handlers ──────────────────────────────
            // All controls save immediately on interaction (no "Save All" button).
            // Toggles revert on failure. Buttons show loading text and re-render
            // the settings page after completion via setTimeout.

            // Poll Now: triggers an IB poll, shows progress, re-renders on complete
            document.getElementById('poll-pause-btn')?.addEventListener('click', async (e) => {
                const btn = e.target;
                btn.disabled = true;
                const isPaused = btn.textContent.trim() === 'Resume Polling';
                btn.textContent = isPaused ? 'Resuming...' : 'Pausing...';
                try {
                    if (isPaused) {
                        await API.resumePolling();
                    } else {
                        await API.pausePolling();
                    }
                    setTimeout(() => this.renderSettings(), 500);
                } catch (err) {
                    btn.textContent = 'Error';
                    alert('Failed: ' + err.message);
                    setTimeout(() => this.renderSettings(), 2000);
                }
            });

            // Session-health card (Settings → Platforms): populate the per-
            // platform session status list + wire the "Check sessions now" button.
            this._initSessionHealthCard();

            document.getElementById('save-all-settings-btn')?.addEventListener('click', async (e) => {
                const btn = e.target;
                btn.disabled = true;
                btn.textContent = 'Saving...';
                try {
                    // Collect all preferences from the form
                    const prefs = {};
                    const val = (id) => document.getElementById(id)?.value;
                    const chk = (id) => document.getElementById(id)?.checked;

                    // General toggles
                    prefs.minimize_to_tray = !!chk('pref-tray');
                    prefs.run_on_startup = !!chk('pref-startup');
                    prefs.notifications_enabled = !!chk('pref-notifications');
                    prefs.watcher_notifications_enabled = !!chk('pref-watcher-notif');
                    if (document.getElementById('pref-logs-panel')) {
                        prefs.logs_panel_enabled = !!chk('pref-logs-panel');
                    }
                    if (document.getElementById('pref-auto-sync')) {
                        prefs.auto_sync_enabled = !!chk('pref-auto-sync');
                    }

                    // Poll intervals
                    prefs.poll_interval_minutes = parseInt(val('pref-poll-interval')) || 60;
                    prefs.fa_poll_interval_minutes = parseInt(val('pref-fa-poll-interval')) || 60;
                    prefs.ws_poll_interval_minutes = parseInt(val('pref-ws-poll-interval')) || 60;
                    prefs.sf_poll_interval_minutes = parseInt(val('pref-sf-poll-interval')) || 60;
                    prefs.sqw_poll_interval_minutes = parseInt(val('pref-sqw-poll-interval')) || 60;
                    prefs.ao3_poll_interval_minutes = parseInt(val('pref-ao3-poll-interval')) || 60;
                    prefs.da_poll_interval_minutes = parseInt(val('pref-da-poll-interval')) || 60;
                    prefs.wp_poll_interval_minutes = parseInt(val('pref-wp-poll-interval')) || 60;
                    prefs.ik_poll_interval_minutes = parseInt(val('pref-ik-poll-interval')) || 60;
                    prefs.bsky_poll_interval_minutes = parseInt(val('pref-bsky-poll-interval')) || 60;
                    prefs.tw_poll_interval_minutes = parseInt(val('pref-tw-poll-interval')) || 60;
                    prefs.mast_poll_interval_minutes = parseInt(val('pref-mast-poll-interval')) || 60;
                    prefs.tum_poll_interval_minutes = parseInt(val('pref-tum-poll-interval')) || 60;
                    prefs.pix_poll_interval_minutes = parseInt(val('pref-pix-poll-interval')) || 60;
                    prefs.thr_poll_interval_minutes = parseInt(val('pref-thr-poll-interval')) || 60;
                    prefs.ig_poll_interval_minutes = parseInt(val('pref-ig-poll-interval')) || 60;
                    prefs.e621_poll_interval_minutes = parseInt(val('pref-e621-poll-interval')) || 60;

                    // Timezone
                    if (val('pref-timezone')) prefs.display_timezone = val('pref-timezone');

                    // Notification filters
                    prefs.notification_comments_only = !!chk('pref-notif-comments-only');
                    prefs.fa_notification_comments_only = !!chk('pref-fa-notif-comments-only');
                    prefs.ws_notification_comments_only = !!chk('pref-ws-notif-comments-only');
                    prefs.sf_notification_comments_only = !!chk('pref-sf-notif-comments-only');
                    prefs.notification_min_views_delta = parseInt(val('pref-min-views-delta')) || 0;
                    prefs.notification_min_faves_delta = parseInt(val('pref-min-faves-delta')) || 0;

                    // Milestones
                    const parseList = (id) => (val(id) || '').split(',').map(s => parseInt(s.trim())).filter(n => n > 0);
                    prefs.milestone_views = parseList('pref-milestone-views');
                    prefs.milestone_faves = parseList('pref-milestone-faves');
                    prefs.milestone_comments = parseList('pref-milestone-comments');

                    // Platform notification toggles
                    if (document.getElementById('pref-fa-notifications')) prefs.fa_notifications_enabled = !!chk('pref-fa-notifications');
                    if (document.getElementById('pref-fa-watcher-notif')) prefs.fa_watcher_notifications_enabled = !!chk('pref-fa-watcher-notif');
                    if (document.getElementById('pref-ws-notifications')) prefs.ws_notifications_enabled = !!chk('pref-ws-notifications');
                    if (document.getElementById('pref-sf-notifications')) prefs.sf_notifications_enabled = !!chk('pref-sf-notifications');
                    if (document.getElementById('pref-sqw-notifications')) prefs.sqw_notifications_enabled = !!chk('pref-sqw-notifications');
                    if (document.getElementById('pref-ao3-notifications')) prefs.ao3_notifications_enabled = !!chk('pref-ao3-notifications');
                    if (document.getElementById('pref-da-notifications')) prefs.da_notifications_enabled = !!chk('pref-da-notifications');
                    if (document.getElementById('pref-wp-notifications')) prefs.wp_notifications_enabled = !!chk('pref-wp-notifications');
                    if (document.getElementById('pref-ik-notifications')) prefs.ik_notifications_enabled = !!chk('pref-ik-notifications');
                    if (document.getElementById('pref-bsky-notifications')) prefs.bsky_notifications_enabled = !!chk('pref-bsky-notifications');
                    if (document.getElementById('pref-tw-notifications')) prefs.tw_notifications_enabled = !!chk('pref-tw-notifications');
                    if (document.getElementById('pref-tw-save-tokens')) prefs.tw_roundrobin_save_tokens = !!chk('pref-tw-save-tokens');
                    if (document.getElementById('pref-mast-notifications')) prefs.mast_notifications_enabled = !!chk('pref-mast-notifications');
                    if (document.getElementById('pref-tum-notifications')) prefs.tum_notifications_enabled = !!chk('pref-tum-notifications');
                    if (document.getElementById('pref-pix-notifications')) prefs.pix_notifications_enabled = !!chk('pref-pix-notifications');
                    if (document.getElementById('pref-thr-notifications')) prefs.thr_notifications_enabled = !!chk('pref-thr-notifications');
                    if (document.getElementById('pref-ig-notifications')) prefs.ig_notifications_enabled = !!chk('pref-ig-notifications');
                    if (document.getElementById('pref-e621-notifications')) prefs.e621_notifications_enabled = !!chk('pref-e621-notifications');
                    if (document.getElementById('pref-fn-notifications')) prefs.fn_notifications_enabled = !!chk('pref-fn-notifications');
                    if (document.getElementById('pref-fbr-notifications')) prefs.fbr_notifications_enabled = !!chk('pref-fbr-notifications');

                    await API.savePreferences(prefs);

                    // Save credentials if username field has a value
                    const username = val('cred-username');
                    const password = document.getElementById('cred-password')?.value;
                    if (username && password) {
                        await API.saveCredentials({ username, password });
                    }

                    btn.textContent = 'Saved!';
                    btn.style.background = 'var(--success)';
                    setTimeout(() => {
                        btn.textContent = 'Save Settings';
                        btn.style.background = '';
                        btn.disabled = false;
                    }, 2000);
                } catch (err) {
                    btn.textContent = 'Error';
                    alert('Save failed: ' + err.message);
                    setTimeout(() => {
                        btn.textContent = 'Save Settings';
                        btn.disabled = false;
                    }, 2000);
                }
            });

            document.getElementById('poll-now-btn')?.addEventListener('click', async (e) => {
                const btn = e.target;
                btn.disabled = true;
                btn.textContent = 'Polling all...';
                try {
                    // Fan out to every CONFIGURED platform, read FRESH from
                    // /platforms/health at click time — a stale _pollingAuth
                    // snapshot could silently drop a configured platform (2.95.0).
                    const TRIGGERS = { ib: 'triggerPoll', fa: 'triggerFAPoll', ws: 'triggerWSPoll',
                        sf: 'triggerSFPoll', sqw: 'triggerSQWPoll', ao3: 'triggerAO3Poll', da: 'triggerDAPoll',
                        wp: 'triggerWPPoll', ik: 'triggerIKPoll', bsky: 'triggerBSKYPoll', tw: 'triggerTWPoll',
                        mast: 'triggerMASTPoll', tum: 'triggerTUMPoll', pix: 'triggerPIXPoll',
                        thr: 'triggerTHRPoll', ig: 'triggerIGPoll', e621: 'triggerE621Poll',
                        fn: 'triggerFNPoll', fbr: 'triggerFBRPoll' };
                    const codes = await this._configuredPollCodes();
                    const triggers = codes.map(c => API[TRIGGERS[c]]());
                    const results = await Promise.allSettled(triggers);
                    const failed = results.filter(r => r.status === 'rejected');
                    btn.textContent = failed.length ? `Done (${failed.length} failed)` : 'Done!';
                    if (window.toast) {
                        if (failed.length) {
                            window.toast.warn(`Polled ${results.length - failed.length}/${results.length} platforms — ${failed.length} failed`);
                        } else {
                            window.toast.success(`Triggered poll on ${results.length} platforms`);
                        }
                    }
                    setTimeout(() => this.renderSettings(), 1500);
                } catch (err) {
                    btn.textContent = 'Error';
                    if (window.toast) window.toast.error(`Poll all failed: ${err.message || err}`);
                    setTimeout(() => this.renderSettings(), 2000);
                }
            });

            // Full Resync: re-scrapes all data for every submission across all platforms.
            // Confirms with the user first since this is a long operation.
            document.getElementById('full-resync-btn')?.addEventListener('click', async (e) => {
                const btn = e.target;
                if (!confirm('Full resync re-scrapes every submission across every connected platform from scratch. This can take 10+ minutes and will hit each platform\'s rate limits hard. Use only when you suspect data is stale. Continue?')) return;
                btn.disabled = true;
                btn.textContent = 'Syncing all...';
                try {
                    // Fan out to every CONFIGURED platform, read FRESH from
                    // /platforms/health (not a stale _pollingAuth snapshot) so a
                    // configured platform can't be silently skipped (2.95.0).
                    const RESYNCS = { ib: 'fullResync', fa: 'fullFAResync', ws: 'fullWSResync',
                        sf: 'fullSFResync', sqw: 'fullSQWResync', ao3: 'fullAO3Resync', da: 'fullDAResync',
                        wp: 'fullWPResync', ik: 'fullIKResync', bsky: 'fullBSKYResync', tw: 'fullTWResync',
                        mast: 'fullMASTResync', tum: 'fullTUMResync', pix: 'fullPIXResync',
                        thr: 'fullTHRResync', ig: 'fullIGResync', e621: 'fullE621Resync',
                        fn: 'fullFNResync', fbr: 'fullFBRResync' };
                    const codes = await this._configuredPollCodes();
                    const resyncs = codes.map(c => API[RESYNCS[c]]());
                    const results = await Promise.allSettled(resyncs);
                    const failed = results.filter(r => r.status === 'rejected');
                    btn.textContent = failed.length ? `Done (${failed.length} failed)` : 'Done!';
                    if (window.toast) {
                        if (failed.length) {
                            window.toast.warn(`Resynced ${results.length - failed.length}/${results.length} platforms — ${failed.length} failed`);
                        } else {
                            window.toast.success(`Triggered full resync on ${results.length} platforms (allow several minutes)`);
                        }
                    }
                    setTimeout(() => this.renderSettings(), 1500);
                } catch (err) {
                    btn.textContent = 'Error';
                    if (window.toast) window.toast.error(`Resync all failed: ${err.message || err}`);
                    setTimeout(() => this.renderSettings(), 2000);
                }
            });

            // Save Credentials: sends username (always) and password (only if changed)
            // to the API. Shows inline success/error message next to the button.
            document.getElementById('save-creds-btn')?.addEventListener('click', async () => {
                const btn = document.getElementById('save-creds-btn');
                const msg = document.getElementById('creds-msg');
                const username = document.getElementById('cred-username').value.trim();
                const password = document.getElementById('cred-password').value;
                if (!username) { msg.textContent = 'Username is required'; msg.style.color = 'var(--danger)'; return; }
                btn.disabled = true;
                btn.textContent = 'Saving...';
                try {
                    const payload = { username };
                    if (password) payload.password = password;
                    await API.saveCredentials(payload);
                    msg.textContent = 'Saved!';
                    msg.style.color = 'var(--success)';
                    btn.textContent = 'Save Credentials';
                    btn.disabled = false;
                } catch (err) {
                    msg.textContent = 'Error: ' + err.message;
                    msg.style.color = 'var(--danger)';
                    btn.textContent = 'Save Credentials';
                    btn.disabled = false;
                }
            });

            // Sign Out: clears IB credentials and navigates to the login page.
            // Button is conditional on creds.has_password — only rendered when
            // there's something to sign out of, so bind defensively.
            document.getElementById('settings-logout-btn')?.addEventListener('click', async () => {
                if (!confirm('Sign out and clear saved credentials?')) return;
                try {
                    await API.authLogout();
                } catch { /* ignore */ }
                this.navigate('/login');
            });

            // Preference toggles — each saves immediately via API. On failure,
            // the toggle reverts to its previous state and shows an alert.
            // The desktop-only ones (tray/startup/notifications) are rendered
            // conditionally based on _isServer, so we use ?. to no-op on the
            // server runtime where those elements don't exist.

            // "Re-run setup" button — clears setup_complete and routes back to
            // the wizard so the user can switch between standalone and paired.
            document.getElementById('btn-rerun-wizard')?.addEventListener('click', async () => {
                if (!confirm('Re-run the setup wizard? This won\'t delete any settings — you\'ll just go back through the questions.')) return;
                try {
                    await API.resetSetupWizard();
                } catch (err) {
                    alert('Failed: ' + err.message);
                    return;
                }
                window.location.hash = '#/setup';
                window.location.reload();
            });

            document.getElementById('pref-tray')?.addEventListener('change', async (e) => {
                try {
                    await API.savePreferences({ minimize_to_tray: e.target.checked });
                } catch (err) {
                    e.target.checked = !e.target.checked;
                    alert('Failed to save preference: ' + err.message);
                }
            });

            // Floating logs button — apply immediately (show/hide) and persist.
            document.getElementById('pref-logs-panel')?.addEventListener('change', async (e) => {
                try { if (window.LogsPanel && LogsPanel.setEnabled) LogsPanel.setEnabled(e.target.checked); } catch (_) { /* widget optional */ }
                try {
                    await API.savePreferences({ logs_panel_enabled: e.target.checked });
                } catch (err) {
                    e.target.checked = !e.target.checked;
                    try { if (window.LogsPanel && LogsPanel.setEnabled) LogsPanel.setEnabled(e.target.checked); } catch (_) {}
                    alert('Failed to save preference: ' + err.message);
                }
            });

            // Auto-sync toggle (Appearance tab — only present when that tab rendered)
            document.getElementById('pref-auto-sync')?.addEventListener('change', async (e) => {
                try {
                    await API.savePreferences({ auto_sync_enabled: e.target.checked });
                } catch (err) {
                    e.target.checked = !e.target.checked;
                    alert('Failed to save preference: ' + err.message);
                }
            });

            document.getElementById('pref-startup')?.addEventListener('change', async (e) => {
                try {
                    await API.savePreferences({ run_on_startup: e.target.checked });
                } catch (err) {
                    e.target.checked = !e.target.checked;
                    alert('Failed to save preference: ' + err.message);
                }
            });

            document.getElementById('pref-notifications')?.addEventListener('change', async (e) => {
                try {
                    await API.savePreferences({ notifications_enabled: e.target.checked });
                } catch (err) {
                    e.target.checked = !e.target.checked;
                    alert('Failed to save preference: ' + err.message);
                }
            });

            document.getElementById('pref-poll-interval')?.addEventListener('change', async (e) => {
                try {
                    await API.savePreferences({ poll_interval_minutes: parseInt(e.target.value) });
                } catch (err) {
                    alert('Failed to save: ' + err.message);
                }
            });

            document.getElementById('pref-fa-poll-interval')?.addEventListener('change', async (e) => {
                try {
                    await API.savePreferences({ fa_poll_interval_minutes: parseInt(e.target.value) });
                } catch (err) {
                    alert('Failed to save: ' + err.message);
                }
            });

            // Clear Session: clears cached API session, forces fresh auth on next request
            document.getElementById('clear-session-btn')?.addEventListener('click', async (e) => {
                const btn = e.target;
                btn.disabled = true;
                btn.textContent = 'Clearing...';
                try {
                    await API.clearSession();
                    btn.textContent = 'Cleared!';
                    setTimeout(() => this.renderSettings(), 1500);
                } catch (err) {
                    btn.textContent = 'Error';
                    alert('Clear session failed: ' + err.message);
                    setTimeout(() => this.renderSettings(), 2000);
                }
            });

            // Telegram Connect: sends bot token to API, validates via getUpdates.
            // Strips API error prefix for cleaner error display. Only rendered
            // when Telegram is not already connected.
            const telegramConnectBtn = document.getElementById('telegram-connect-btn');
            if (telegramConnectBtn) {
                telegramConnectBtn.addEventListener('click', async () => {
                    const btn = telegramConnectBtn;
                    const msg = document.getElementById('telegram-msg');
                    const token = document.getElementById('telegram-token').value.trim();
                    if (!token) { msg.textContent = 'Paste your bot token first'; msg.style.color = 'var(--danger)'; return; }
                    btn.disabled = true;
                    btn.textContent = 'Connecting...';
                    msg.textContent = '';
                    try {
                        await API.connectTelegram({ bot_token: token });
                        msg.textContent = 'Connected!';
                        msg.style.color = 'var(--success)';
                        setTimeout(() => this.renderSettings(), 1000);
                    } catch (err) {
                        let detail = err.message.replace(/^API \d+:\s*/, '');
                        try { detail = JSON.parse(detail).detail || detail; } catch {}
                        msg.textContent = detail;
                        msg.style.color = 'var(--danger)';
                        btn.textContent = 'Connect';
                        btn.disabled = false;
                    }
                });
            }

            // Telegram Test: sends a test message to verify the integration works
            const telegramTestBtn = document.getElementById('telegram-test-btn');
            if (telegramTestBtn) {
                telegramTestBtn.addEventListener('click', async () => {
                    const btn = telegramTestBtn;
                    const msg = document.getElementById('telegram-msg');
                    btn.disabled = true;
                    btn.textContent = 'Sending...';
                    msg.textContent = '';
                    try {
                        await API.testTelegram();
                        msg.textContent = 'Test message sent!';
                        msg.style.color = 'var(--success)';
                        btn.textContent = 'Test';
                        btn.disabled = false;
                    } catch (err) {
                        msg.textContent = 'Failed: ' + err.message;
                        msg.style.color = 'var(--danger)';
                        btn.textContent = 'Test';
                        btn.disabled = false;
                    }
                });
            }

            // Telegram Disconnect: removes bot token/chat_id, re-renders to show connect form
            const telegramDisconnectBtn = document.getElementById('telegram-disconnect-btn');
            if (telegramDisconnectBtn) {
                telegramDisconnectBtn.addEventListener('click', async () => {
                    if (!confirm('Disconnect Telegram notifications?')) return;
                    try {
                        await API.disconnectTelegram();
                        this.renderSettings();
                    } catch (err) {
                        alert('Failed: ' + err.message);
                    }
                });
            }

            // Telegram Enable/Disable: controls whether notifications are sent
            // (separate from being connected -- can be connected but disabled)
            const telegramToggle = document.getElementById('pref-telegram');
            if (telegramToggle) {
                telegramToggle.addEventListener('change', async (e) => {
                    try {
                        await API.savePreferences({ telegram_enabled: e.target.checked });
                    } catch (err) {
                        e.target.checked = !e.target.checked;
                        alert('Failed to save preference: ' + err.message);
                    }
                });
            }

            // Telegram feature toggles: save immediately on change
            for (const [elId, key] of [['tg-feat-summaries','poll_summaries'],['tg-feat-errors','error_alerts'],['tg-feat-milestones','milestones'],['tg-feat-digest','digest']]) {
                const el = document.getElementById(elId);
                if (el) el.addEventListener('change', async (e) => {
                    try { await API.setTelegramFeatures({ [key]: e.target.checked }); }
                    catch (err) { e.target.checked = !e.target.checked; alert('Failed: ' + err.message); }
                });
            }

            // Digest interval: save on change with debounce
            const digestIntervalInput = document.getElementById('tg-digest-interval');
            if (digestIntervalInput) {
                let _digestTimeout;
                digestIntervalInput.addEventListener('change', async () => {
                    clearTimeout(_digestTimeout);
                    const val = Math.max(1, Math.min(168, parseInt(digestIntervalInput.value) || 6));
                    digestIntervalInput.value = val;
                    try { await API.setTelegramFeatures({ digest_interval_hours: val }); }
                    catch (err) { alert('Failed: ' + err.message); }
                });
            }

            // ── Weekly email digest ───────────────────────────────────
            const digestSaveBtn = document.getElementById('digest-save-btn');
            if (digestSaveBtn) {
                const dMsg = () => document.getElementById('digest-msg');
                const collectDigest = () => {
                    const payload = {
                        email_digest_enabled: document.getElementById('digest-enabled').checked,
                        email_digest_recipients: document.getElementById('digest-recipients').value.trim(),
                        email_digest_interval_days: Math.max(1, Math.min(90, parseInt(document.getElementById('digest-interval').value) || 7)),
                        smtp_host: document.getElementById('digest-smtp-host').value.trim(),
                        smtp_port: parseInt(document.getElementById('digest-smtp-port').value) || 587,
                        smtp_use_tls: document.getElementById('digest-smtp-tls').checked,
                        smtp_username: document.getElementById('digest-smtp-user').value.trim(),
                        smtp_from: document.getElementById('digest-smtp-from').value.trim(),
                    };
                    const pass = document.getElementById('digest-smtp-pass').value;
                    if (pass) payload.smtp_password = pass;   // blank = keep existing
                    return payload;
                };
                digestSaveBtn.addEventListener('click', async () => {
                    const msg = dMsg();
                    digestSaveBtn.disabled = true;
                    try {
                        await API.saveDigestSettings(collectDigest());
                        msg.textContent = 'Saved.';
                        msg.style.color = 'var(--success)';
                        document.getElementById('digest-smtp-pass').value = '';
                    } catch (err) {
                        msg.textContent = 'Failed: ' + err.message;
                        msg.style.color = 'var(--danger)';
                    }
                    digestSaveBtn.disabled = false;
                });

                const digestTestBtn = document.getElementById('digest-test-btn');
                digestTestBtn.addEventListener('click', async () => {
                    const msg = dMsg();
                    // Save current form first so the test uses what's on screen.
                    digestTestBtn.disabled = true;
                    digestTestBtn.textContent = 'Sending...';
                    msg.textContent = '';
                    try {
                        await API.saveDigestSettings(collectDigest());
                        document.getElementById('digest-smtp-pass').value = '';
                        const res = await API.testDigest();
                        msg.textContent = 'Test sent to ' + (res.recipients || []).join(', ');
                        msg.style.color = 'var(--success)';
                    } catch (err) {
                        msg.textContent = 'Failed: ' + err.message;
                        msg.style.color = 'var(--danger)';
                    }
                    digestTestBtn.textContent = 'Send test now';
                    digestTestBtn.disabled = false;
                });

                const digestPreviewBtn = document.getElementById('digest-preview-btn');
                digestPreviewBtn.addEventListener('click', () => {
                    window.open('/api/digest/preview', '_blank');
                });
            }

            // ── Telegram channel posting ──────────────────────────────
            const tgchanSave = document.getElementById('tgchan-save');
            if (tgchanSave) {
                const tgMsg = () => document.getElementById('tgchan-msg');
                const collectTg = () => {
                    const payload = { channel: document.getElementById('tgchan-channel').value.trim(),
                        protect:  document.getElementById('tgchan-protect').checked,
                        document: document.getElementById('tgchan-document').checked,
                        silent:   document.getElementById('tgchan-silent').checked,
                        no_tags:  document.getElementById('tgchan-notags').checked };
                    const tok = document.getElementById('tgchan-token').value;
                    if (tok) payload.bot_token = tok;   // blank = keep existing
                    return payload;
                };
                tgchanSave.addEventListener('click', async () => {
                    const msg = tgMsg();
                    tgchanSave.disabled = true;
                    try {
                        await API.saveTelegramChannel(collectTg());
                        document.getElementById('tgchan-token').value = '';
                        msg.textContent = 'Saved.';
                        msg.style.color = 'var(--success)';
                    } catch (err) {
                        msg.textContent = 'Failed: ' + err.message;
                        msg.style.color = 'var(--danger)';
                    }
                    tgchanSave.disabled = false;
                });
                /* Find my channel — ask Telegram which channels this bot can see.
                 *
                 * A private channel has no username, and its numeric -100… id is
                 * never shown in Telegram's UI. Typing the title instead sends us
                 * to "@title", which may be a STRANGER's public channel — that
                 * passes the getChat check and then refuses the post. Letting the
                 * bot identify the channel removes the guess entirely. */
                const tgchanDetect = document.getElementById('tgchan-detect');
                if (tgchanDetect) tgchanDetect.addEventListener('click', async () => {
                    const msg = tgMsg();
                    tgchanDetect.disabled = true;
                    const label = tgchanDetect.innerHTML;
                    tgchanDetect.textContent = 'Looking…';
                    msg.textContent = '';
                    try {
                        const res = await API.detectTelegramChannels();
                        const list = res.channels || [];
                        if (!list.length) throw new Error('No channels found');
                        // One match fills it silently; several need a choice, and
                        // the id alone is unreadable — show the title with it.
                        const pick = list.length === 1 ? list[0] : (() => {
                            const lines = list.map((c, i) =>
                                `${i + 1}. ${c.title || '(untitled)'}${c.username ? ' @' + c.username : ''}  [${c.id}]`);
                            const n = prompt('Which channel?\n\n' + lines.join('\n'), '1');
                            const idx = parseInt(n, 10) - 1;
                            return (idx >= 0 && idx < list.length) ? list[idx] : null;
                        })();
                        if (!pick) { msg.textContent = ''; return; }
                        // Prefer the public @username when there is one: it stays
                        // valid if the channel is ever recreated, and it reads.
                        document.getElementById('tgchan-channel').value =
                            pick.username ? '@' + pick.username : pick.id;
                        msg.textContent = `Found “${pick.title || pick.id}” — press Save & send test to confirm.`;
                        msg.style.color = 'var(--success)';
                    } catch (err) {
                        msg.textContent = 'Failed: ' + err.message.replace(/^API \d+:\s*/, '');
                        msg.style.color = 'var(--danger)';
                    } finally {
                        tgchanDetect.disabled = false;
                        tgchanDetect.innerHTML = label;
                    }
                });

                const tgchanTest = document.getElementById('tgchan-test');
                tgchanTest.addEventListener('click', async () => {
                    const msg = tgMsg();
                    tgchanTest.disabled = true;
                    tgchanTest.textContent = 'Testing…';
                    msg.textContent = '';
                    try {
                        await API.saveTelegramChannel(collectTg());
                        document.getElementById('tgchan-token').value = '';
                        const res = await API.testTelegramChannel({ channel: document.getElementById('tgchan-channel').value.trim() });
                        msg.textContent = res.message || 'Test posted.';
                        msg.style.color = 'var(--success)';
                    } catch (err) {
                        msg.textContent = 'Failed: ' + err.message;
                        msg.style.color = 'var(--danger)';
                    }
                    tgchanTest.textContent = 'Save & send test';
                    tgchanTest.disabled = false;
                });
            }

            // ── Browser Login buttons ─────────────────────────────────
            // Toggle manual-entry sections when "Enter cookies manually" is clicked.
            document.getElementById('fa-manual-toggle')?.addEventListener('click', () => {
                const section = document.getElementById('fa-manual-section');
                if (section) section.style.display = section.style.display === 'none' ? '' : 'none';
            });
            document.getElementById('da-manual-toggle')?.addEventListener('click', () => {
                const section = document.getElementById('da-manual-section');
                if (section) section.style.display = section.style.display === 'none' ? '' : 'none';
            });
            document.getElementById('tw-manual-toggle')?.addEventListener('click', () => {
                const section = document.getElementById('tw-manual-section');
                if (section) section.style.display = section.style.display === 'none' ? '' : 'none';
            });

            // FA Browser Login: opens pywebview popup, auto-captures cookies
            const faBrowserLoginBtn = document.getElementById('fa-browser-login-btn');
            if (faBrowserLoginBtn) {
                faBrowserLoginBtn.addEventListener('click', async () => {
                    const msg = document.getElementById('fa-msg');
                    const username = document.getElementById('fa-browser-username')?.value.trim();
                    if (!username) {
                        if (msg) { msg.textContent = 'Username is required'; msg.style.color = 'var(--danger)'; }
                        return;
                    }
                    faBrowserLoginBtn.disabled = true;
                    faBrowserLoginBtn.textContent = 'Waiting for login...';
                    if (msg) { msg.textContent = 'A login window will open. Log in to FurAffinity, then it will close automatically.'; msg.style.color = 'var(--text-muted)'; }
                    try {
                        const result = await API.browserLogin('fa', { fa_username: username });
                        if (result.ok) {
                            if (msg) { msg.textContent = 'Connected!'; msg.style.color = 'var(--success)'; }
                            setTimeout(() => this.renderSettings(), 1000);
                        } else {
                            if (msg) { msg.textContent = result.message || 'Login cancelled.'; msg.style.color = 'var(--text-muted)'; }
                            faBrowserLoginBtn.textContent = 'Login via Browser';
                            faBrowserLoginBtn.disabled = false;
                        }
                    } catch (err) {
                        let detail = err.message.replace(/^API \d+:\s*/, '');
                        try { detail = JSON.parse(detail).detail || detail; } catch {}
                        if (msg) { msg.textContent = detail; msg.style.color = 'var(--danger)'; }
                        faBrowserLoginBtn.textContent = 'Login via Browser';
                        faBrowserLoginBtn.disabled = false;
                    }
                });
            }

            // DA Browser Login: opens pywebview popup for DeviantArt
            const daBrowserLoginBtn = document.getElementById('da-browser-login-btn');
            if (daBrowserLoginBtn) {
                daBrowserLoginBtn.addEventListener('click', async () => {
                    const msg = document.getElementById('da-msg');
                    const username = document.getElementById('da-browser-username')?.value.trim();
                    if (!username) {
                        if (msg) { msg.textContent = 'Username is required'; msg.style.color = 'var(--danger)'; }
                        return;
                    }
                    daBrowserLoginBtn.disabled = true;
                    daBrowserLoginBtn.textContent = 'Waiting for login...';
                    if (msg) { msg.textContent = 'A login window will open. Log in to DeviantArt, then it will close automatically.'; msg.style.color = 'var(--text-muted)'; }
                    try {
                        const result = await API.browserLogin('da', { da_username: username });
                        if (result.ok) {
                            if (msg) { msg.textContent = 'Connected!'; msg.style.color = 'var(--success)'; }
                            setTimeout(() => this.renderSettings(), 1000);
                        } else {
                            if (msg) { msg.textContent = result.message || 'Login cancelled.'; msg.style.color = 'var(--text-muted)'; }
                            daBrowserLoginBtn.textContent = 'Login via Browser';
                            daBrowserLoginBtn.disabled = false;
                        }
                    } catch (err) {
                        let detail = err.message.replace(/^API \d+:\s*/, '');
                        try { detail = JSON.parse(detail).detail || detail; } catch {}
                        if (msg) { msg.textContent = detail; msg.style.color = 'var(--danger)'; }
                        daBrowserLoginBtn.textContent = 'Login via Browser';
                        daBrowserLoginBtn.disabled = false;
                    }
                });
            }

            // TW Browser Login: opens pywebview popup for X/Twitter
            const twBrowserLoginBtn = document.getElementById('tw-browser-login-btn');
            if (twBrowserLoginBtn) {
                twBrowserLoginBtn.addEventListener('click', async () => {
                    const msg = document.getElementById('tw-msg');
                    const username = document.getElementById('tw-browser-username')?.value.trim();
                    if (!username) {
                        if (msg) { msg.textContent = 'Username is required'; msg.style.color = 'var(--danger)'; }
                        return;
                    }
                    twBrowserLoginBtn.disabled = true;
                    twBrowserLoginBtn.textContent = 'Waiting for login...';
                    if (msg) { msg.textContent = 'A login window will open. Log in to X, then it will close automatically.'; msg.style.color = 'var(--text-muted)'; }
                    try {
                        const result = await API.browserLogin('tw', { tw_username: username });
                        if (result.ok) {
                            if (msg) { msg.textContent = 'Connected!'; msg.style.color = 'var(--success)'; }
                            setTimeout(() => this.renderSettings(), 1000);
                        } else {
                            if (msg) { msg.textContent = result.message || 'Login cancelled.'; msg.style.color = 'var(--text-muted)'; }
                            twBrowserLoginBtn.textContent = 'Login via Browser';
                            twBrowserLoginBtn.disabled = false;
                        }
                    } catch (err) {
                        let detail = err.message.replace(/^API \d+:\s*/, '');
                        try { detail = JSON.parse(detail).detail || detail; } catch {}
                        if (msg) { msg.textContent = detail; msg.style.color = 'var(--danger)'; }
                        twBrowserLoginBtn.textContent = 'Login via Browser';
                        twBrowserLoginBtn.disabled = false;
                    }
                });
            }

            // IB Browser Login: verification-only — opens inkbunny.net so the
            // user can confirm their credentials work in a real browser.  IB's
            // API needs username + password to mint an SID via api_login.php,
            // so web cookies aren't usable for auth and nothing is saved by
            // the browser-login flow itself.
            const ibBrowserLoginBtn = document.getElementById('ib-browser-login-btn');
            if (ibBrowserLoginBtn) {
                ibBrowserLoginBtn.addEventListener('click', async () => {
                    const msg = document.getElementById('creds-msg');
                    ibBrowserLoginBtn.disabled = true;
                    ibBrowserLoginBtn.textContent = 'Opening...';
                    if (msg) { msg.textContent = 'A login window will open. Log in to Inkbunny to verify your credentials.'; msg.style.color = 'var(--text-muted)'; }
                    try {
                        const result = await API.browserLogin('ib', {});
                        if (result.ok) {
                            if (msg) { msg.textContent = 'Verified — Inkbunny accepted the login. Save your username and password above so the poller can authenticate.'; msg.style.color = 'var(--success)'; }
                        } else {
                            if (msg) { msg.textContent = result.message || 'Login window closed.'; msg.style.color = 'var(--text-muted)'; }
                        }
                    } catch (err) {
                        let detail = err.message.replace(/^API \d+:\s*/, '');
                        try { detail = JSON.parse(detail).detail || detail; } catch {}
                        if (msg) { msg.textContent = detail; msg.style.color = 'var(--danger)'; }
                    }
                    ibBrowserLoginBtn.textContent = 'Verify in Browser';
                    ibBrowserLoginBtn.disabled = false;
                });
            }

            // FA Connect: sends username + cookie_a + cookie_b to authenticate
            // with FurAffinity via browser cookies. All three fields required.
            const faConnectBtn = document.getElementById('fa-connect-btn');
            if (faConnectBtn) {
                faConnectBtn.addEventListener('click', async () => {
                    const msg = document.getElementById('fa-msg');
                    const username = document.getElementById('fa-username').value.trim();
                    const cookieA = document.getElementById('fa-cookie-a').value.trim();
                    const cookieB = document.getElementById('fa-cookie-b').value.trim();
                    if (!username || !cookieA || !cookieB) {
                        msg.textContent = 'All three fields are required';
                        msg.style.color = 'var(--danger)';
                        return;
                    }
                    faConnectBtn.disabled = true;
                    faConnectBtn.textContent = 'Connecting...';
                    msg.textContent = '';
                    try {
                        await API.faConnect({ username, cookie_a: cookieA, cookie_b: cookieB });
                        msg.textContent = 'Connected!';
                        msg.style.color = 'var(--success)';
                        setTimeout(() => this.renderSettings(), 1000);
                    } catch (err) {
                        let detail = err.message.replace(/^API \d+:\s*/, '');
                        try { detail = JSON.parse(detail).detail || detail; } catch {}
                        msg.textContent = detail;
                        msg.style.color = 'var(--danger)';
                        faConnectBtn.textContent = 'Connect';
                        faConnectBtn.disabled = false;
                    }
                });
            }

            // FA Disconnect: clears saved cookies, re-renders to show connect form
            const faDisconnectBtn = document.getElementById('fa-disconnect-btn');
            if (faDisconnectBtn) {
                faDisconnectBtn.addEventListener('click', async () => {
                    if (!confirm('Disconnect FurAffinity? This clears saved cookies.')) return;
                    try {
                        await API.faDisconnect();
                        this.renderSettings();
                    } catch (err) {
                        alert('Failed: ' + err.message);
                    }
                });
            }

            // FA Poll Now / Full Resync — see _pollingTabPoll/_pollingTabResync
            // for the shared implementation (toast feedback + inline msg + confirm).
            const faPollBtn = document.getElementById('fa-poll-btn');
            if (faPollBtn) {
                faPollBtn.addEventListener('click', () => this._pollingTabPoll({
                    btn: faPollBtn, msgId: 'fa-msg', platform: 'fa', apiMethod: 'triggerFAPoll',
                }));
            }
            const faResyncBtn = document.getElementById('fa-resync-btn');
            if (faResyncBtn) {
                faResyncBtn.addEventListener('click', () => this._pollingTabResync({
                    btn: faResyncBtn, msgId: 'fa-msg', platform: 'fa', apiMethod: 'fullFAResync',
                }));
            }

            // FA Notifications toggle: enables/disables FA desktop + Telegram alerts
            const faNotifToggle = document.getElementById('pref-fa-notifications');
            if (faNotifToggle) {
                faNotifToggle.addEventListener('change', async (e) => {
                    try {
                        await API.savePreferences({ fa_notifications_enabled: e.target.checked });
                    } catch (err) {
                        e.target.checked = !e.target.checked;
                        alert('Failed to save preference: ' + err.message);
                    }
                });
            }

            // WS poll interval dropdown (15/30/60/120/240 minutes)
            document.getElementById('pref-ws-poll-interval')?.addEventListener('change', async (e) => {
                try {
                    await API.savePreferences({ ws_poll_interval_minutes: parseInt(e.target.value) });
                } catch (err) {
                    alert('Failed to save: ' + err.message);
                }
            });

            // Watcher notification toggles
            document.getElementById('pref-watcher-notif')?.addEventListener('change', async (e) => {
                try { await API.savePreferences({ watcher_notifications_enabled: e.target.checked }); }
                catch (err) { e.target.checked = !e.target.checked; alert('Failed: ' + err.message); }
            });
            document.getElementById('pref-fa-watcher-notif')?.addEventListener('change', async (e) => {
                try { await API.savePreferences({ fa_watcher_notifications_enabled: e.target.checked }); }
                catch (err) { e.target.checked = !e.target.checked; alert('Failed: ' + err.message); }
            });

            // Notification filter toggles
            document.getElementById('pref-notif-comments-only')?.addEventListener('change', async (e) => {
                try { await API.savePreferences({ notification_comments_only: e.target.checked }); }
                catch (err) { e.target.checked = !e.target.checked; alert('Failed: ' + err.message); }
            });
            document.getElementById('pref-fa-notif-comments-only')?.addEventListener('change', async (e) => {
                try { await API.savePreferences({ fa_notification_comments_only: e.target.checked }); }
                catch (err) { e.target.checked = !e.target.checked; alert('Failed: ' + err.message); }
            });
            document.getElementById('pref-ws-notif-comments-only')?.addEventListener('change', async (e) => {
                try { await API.savePreferences({ ws_notification_comments_only: e.target.checked }); }
                catch (err) { e.target.checked = !e.target.checked; alert('Failed: ' + err.message); }
            });
            document.getElementById('pref-min-views-delta')?.addEventListener('change', async (e) => {
                try { await API.savePreferences({ notification_min_views_delta: parseInt(e.target.value) || 0 }); }
                catch (err) { alert('Failed: ' + err.message); }
            });
            document.getElementById('pref-min-faves-delta')?.addEventListener('change', async (e) => {
                try { await API.savePreferences({ notification_min_faves_delta: parseInt(e.target.value) || 0 }); }
                catch (err) { alert('Failed: ' + err.message); }
            });

            // WS: Connect
            const wsConnectBtn = document.getElementById('ws-connect-btn');
            if (wsConnectBtn) {
                wsConnectBtn.addEventListener('click', async () => {
                    const msg = document.getElementById('ws-msg');
                    const apiKey = document.getElementById('ws-api-key').value.trim();
                    if (!apiKey) { msg.textContent = 'API key is required'; msg.style.color = 'var(--danger)'; return; }
                    wsConnectBtn.disabled = true;
                    wsConnectBtn.textContent = 'Connecting...';
                    msg.textContent = '';
                    try {
                        await API.wsConnect({ api_key: apiKey });
                        msg.textContent = 'Connected!';
                        msg.style.color = 'var(--success)';
                        setTimeout(() => this.renderSettings(), 1000);
                    } catch (err) {
                        let detail = err.message.replace(/^API \d+:\s*/, '');
                        try { detail = JSON.parse(detail).detail || detail; } catch {}
                        msg.textContent = detail;
                        msg.style.color = 'var(--danger)';
                        wsConnectBtn.textContent = 'Connect';
                        wsConnectBtn.disabled = false;
                    }
                });
            }

            // WS: Disconnect
            const wsDisconnectBtn = document.getElementById('ws-disconnect-btn');
            if (wsDisconnectBtn) {
                wsDisconnectBtn.addEventListener('click', async () => {
                    if (!confirm('Disconnect Weasyl? This clears saved API key.')) return;
                    try {
                        await API.wsDisconnect();
                        this.renderSettings();
                    } catch (err) {
                        alert('Failed: ' + err.message);
                    }
                });
            }

            // WS: Poll Now / Full Resync — see _pollingTabPoll/_pollingTabResync
            const wsPollBtn = document.getElementById('ws-poll-btn');
            if (wsPollBtn) {
                wsPollBtn.addEventListener('click', () => this._pollingTabPoll({
                    btn: wsPollBtn, msgId: 'ws-msg', platform: 'ws', apiMethod: 'triggerWSPoll',
                }));
            }
            const wsResyncBtn = document.getElementById('ws-resync-btn');
            if (wsResyncBtn) {
                wsResyncBtn.addEventListener('click', () => this._pollingTabResync({
                    btn: wsResyncBtn, msgId: 'ws-msg', platform: 'ws', apiMethod: 'fullWSResync',
                }));
            }

            // WS: Notifications toggle
            const wsNotifToggle = document.getElementById('pref-ws-notifications');
            if (wsNotifToggle) {
                wsNotifToggle.addEventListener('change', async (e) => {
                    try {
                        await API.savePreferences({ ws_notifications_enabled: e.target.checked });
                    } catch (err) {
                        e.target.checked = !e.target.checked;
                        alert('Failed to save preference: ' + err.message);
                    }
                });
            }

            // SF poll interval dropdown
            document.getElementById('pref-sf-poll-interval')?.addEventListener('change', async (e) => {
                try {
                    await API.savePreferences({ sf_poll_interval_minutes: parseInt(e.target.value) });
                } catch (err) {
                    alert('Failed to save: ' + err.message);
                }
            });

            // SqW poll interval dropdown
            document.getElementById('pref-sqw-poll-interval')?.addEventListener('change', async (e) => {
                try {
                    await API.savePreferences({ sqw_poll_interval_minutes: parseInt(e.target.value) });
                } catch (err) {
                    alert('Failed to save: ' + err.message);
                }
            });

            // AO3 poll interval dropdown
            document.getElementById('pref-ao3-poll-interval')?.addEventListener('change', async (e) => {
                try {
                    await API.savePreferences({ ao3_poll_interval_minutes: parseInt(e.target.value) });
                } catch (err) {
                    alert('Failed to save: ' + err.message);
                }
            });

            // DA poll interval dropdown
            document.getElementById('pref-da-poll-interval')?.addEventListener('change', async (e) => {
                try {
                    await API.savePreferences({ da_poll_interval_minutes: parseInt(e.target.value) });
                } catch (err) {
                    alert('Failed to save: ' + err.message);
                }
            });

            // WP poll interval dropdown
            document.getElementById('pref-wp-poll-interval')?.addEventListener('change', async (e) => {
                try {
                    await API.savePreferences({ wp_poll_interval_minutes: parseInt(e.target.value) });
                } catch (err) {
                    alert('Failed to save: ' + err.message);
                }
            });

            // IK poll interval dropdown
            document.getElementById('pref-ik-poll-interval')?.addEventListener('change', async (e) => {
                try {
                    await API.savePreferences({ ik_poll_interval_minutes: parseInt(e.target.value) });
                } catch (err) {
                    alert('Failed to save: ' + err.message);
                }
            });

            // BSKY poll interval dropdown
            document.getElementById('pref-bsky-poll-interval')?.addEventListener('change', async (e) => {
                try {
                    await API.savePreferences({ bsky_poll_interval_minutes: parseInt(e.target.value) });
                } catch (err) {
                    alert('Failed to save: ' + err.message);
                }
            });

            // TW poll interval dropdown
            document.getElementById('pref-tw-poll-interval')?.addEventListener('change', async (e) => {
                try {
                    await API.savePreferences({ tw_poll_interval_minutes: parseInt(e.target.value) });
                } catch (err) {
                    alert('Failed to save: ' + err.message);
                }
            });

            // MAST poll interval dropdown
            document.getElementById('pref-mast-poll-interval')?.addEventListener('change', async (e) => {
                try {
                    await API.savePreferences({ mast_poll_interval_minutes: parseInt(e.target.value) });
                } catch (err) {
                    alert('Failed to save: ' + err.message);
                }
            });

            // TUM poll interval dropdown
            document.getElementById('pref-tum-poll-interval')?.addEventListener('change', async (e) => {
                try {
                    await API.savePreferences({ tum_poll_interval_minutes: parseInt(e.target.value) });
                } catch (err) {
                    alert('Failed to save: ' + err.message);
                }
            });

            // PIX poll interval dropdown
            document.getElementById('pref-pix-poll-interval')?.addEventListener('change', async (e) => {
                try {
                    await API.savePreferences({ pix_poll_interval_minutes: parseInt(e.target.value) });
                } catch (err) {
                    alert('Failed to save: ' + err.message);
                }
            });

            // THR poll interval dropdown
            document.getElementById('pref-thr-poll-interval')?.addEventListener('change', async (e) => {
                try {
                    await API.savePreferences({ thr_poll_interval_minutes: parseInt(e.target.value) });
                } catch (err) {
                    alert('Failed to save: ' + err.message);
                }
            });

            // IG poll interval dropdown
            document.getElementById('pref-ig-poll-interval')?.addEventListener('change', async (e) => {
                try {
                    await API.savePreferences({ ig_poll_interval_minutes: parseInt(e.target.value) });
                } catch (err) {
                    alert('Failed to save: ' + err.message);
                }
            });

            // e621 poll interval dropdown
            document.getElementById('pref-e621-poll-interval')?.addEventListener('change', async (e) => {
                try {
                    await API.savePreferences({ e621_poll_interval_minutes: parseInt(e.target.value) });
                } catch (err) {
                    alert('Failed to save: ' + err.message);
                }
            });

            // "Set all platforms" — applies one interval to every platform in a
            // single save, then mirrors the new value into each per-platform
            // dropdown so the UI stays in sync. Keys mirror the backend's
            // _PLATFORM_HEALTH_CONFIG order (IB has no prefix).
            const _ALL_INTERVAL_IDS = [
                'pref-poll-interval', 'pref-fa-poll-interval', 'pref-ws-poll-interval',
                'pref-sf-poll-interval', 'pref-sqw-poll-interval', 'pref-ao3-poll-interval',
                'pref-da-poll-interval', 'pref-wp-poll-interval', 'pref-ik-poll-interval',
                'pref-bsky-poll-interval', 'pref-tw-poll-interval', 'pref-mast-poll-interval',
                'pref-tum-poll-interval', 'pref-pix-poll-interval', 'pref-thr-poll-interval',
                'pref-ig-poll-interval', 'pref-e621-poll-interval',
            ];
            const _ALL_INTERVAL_KEYS = [
                'poll_interval_minutes', 'fa_poll_interval_minutes', 'ws_poll_interval_minutes',
                'sf_poll_interval_minutes', 'sqw_poll_interval_minutes', 'ao3_poll_interval_minutes',
                'da_poll_interval_minutes', 'wp_poll_interval_minutes', 'ik_poll_interval_minutes',
                'bsky_poll_interval_minutes', 'tw_poll_interval_minutes', 'mast_poll_interval_minutes',
                'tum_poll_interval_minutes', 'pix_poll_interval_minutes', 'thr_poll_interval_minutes',
                'ig_poll_interval_minutes', 'e621_poll_interval_minutes',
            ];
            document.getElementById('pref-poll-interval-all')?.addEventListener('change', async (e) => {
                const v = parseInt(e.target.value);
                if (!v) return;
                const payload = {};
                _ALL_INTERVAL_KEYS.forEach(k => { payload[k] = v; });
                try {
                    await API.savePreferences(payload);
                    // mirror the applied value into every per-platform dropdown
                    _ALL_INTERVAL_IDS.forEach(id => {
                        const el = document.getElementById(id);
                        if (el) el.value = String(v);
                    });
                    e.target.value = '';  // reset back to the placeholder
                    if (window.toast) window.toast.success('All 17 platforms set to ' + (v >= 60 ? (v / 60) + 'h' : v + ' min'));
                } catch (err) {
                    e.target.value = '';
                    alert('Failed to save: ' + err.message);
                }
            });

            // Display timezone dropdown
            document.getElementById('pref-timezone')?.addEventListener('change', async (e) => {
                try {
                    await API.savePreferences({ display_timezone: e.target.value });
                } catch (err) {
                    alert('Failed to save: ' + err.message);
                }
            });

            // SF notification filter toggle
            document.getElementById('pref-sf-notif-comments-only')?.addEventListener('change', async (e) => {
                try { await API.savePreferences({ sf_notification_comments_only: e.target.checked }); }
                catch (err) { e.target.checked = !e.target.checked; alert('Failed: ' + err.message); }
            });

            // SF: Connect
            const sfConnectBtn = document.getElementById('sf-connect-btn');
            if (sfConnectBtn) {
                sfConnectBtn.addEventListener('click', async () => {
                    const msg = document.getElementById('sf-msg');
                    const api_token = (document.getElementById('sf-api-token')?.value || '').trim();
                    if (!api_token) {
                        msg.textContent = 'Paste your SoFurry Personal Access Token';
                        msg.style.color = 'var(--danger)';
                        return;
                    }
                    sfConnectBtn.disabled = true;
                    sfConnectBtn.textContent = 'Connecting...';
                    msg.textContent = '';
                    try {
                        await API.sfConnect({ api_token });
                        msg.textContent = 'Connected!';
                        msg.style.color = 'var(--success)';
                        setTimeout(() => this.renderSettings(), 1000);
                    } catch (err) {
                        let detail = err.message.replace(/^API \d+:\s*/, '');
                        try { detail = JSON.parse(detail).detail || detail; } catch {}
                        msg.textContent = detail;
                        msg.style.color = 'var(--danger)';
                        sfConnectBtn.textContent = 'Connect';
                        sfConnectBtn.disabled = false;
                    }
                });
            }

            // SF: Disconnect
            const sfDisconnectBtn = document.getElementById('sf-disconnect-btn');
            if (sfDisconnectBtn) {
                sfDisconnectBtn.addEventListener('click', async () => {
                    if (!confirm('Disconnect SoFurry? This clears saved credentials.')) return;
                    try {
                        await API.sfDisconnect();
                        this.renderSettings();
                    } catch (err) {
                        alert('Failed: ' + err.message);
                    }
                });
            }

            // SF: Poll Now / Full Resync — see _pollingTabPoll/_pollingTabResync
            const sfPollBtn = document.getElementById('sf-poll-btn');
            if (sfPollBtn) {
                sfPollBtn.addEventListener('click', () => this._pollingTabPoll({
                    btn: sfPollBtn, msgId: 'sf-msg', platform: 'sf', apiMethod: 'triggerSFPoll',
                }));
            }
            const sfResyncBtn = document.getElementById('sf-resync-btn');
            if (sfResyncBtn) {
                sfResyncBtn.addEventListener('click', () => this._pollingTabResync({
                    btn: sfResyncBtn, msgId: 'sf-msg', platform: 'sf', apiMethod: 'fullSFResync',
                }));
            }

            // SF: Notifications toggle
            const sfNotifToggle = document.getElementById('pref-sf-notifications');
            if (sfNotifToggle) {
                sfNotifToggle.addEventListener('change', async (e) => {
                    try {
                        await API.savePreferences({ sf_notifications_enabled: e.target.checked });
                    } catch (err) {
                        e.target.checked = !e.target.checked;
                        alert('Failed to save preference: ' + err.message);
                    }
                });
            }

            // SQW: Connect
            const sqwConnectBtn = document.getElementById('sqw-connect-btn');
            if (sqwConnectBtn) {
                sqwConnectBtn.addEventListener('click', async () => {
                    const msg = document.getElementById('sqw-msg');
                    const username = document.getElementById('sqw-username').value.trim();
                    const password = document.getElementById('sqw-password').value;
                    const target_user = document.getElementById('sqw-target-user').value.trim();
                    if (!username || !password || !target_user) {
                        msg.textContent = 'All fields required';
                        msg.style.color = 'var(--danger)';
                        return;
                    }
                    sqwConnectBtn.disabled = true;
                    sqwConnectBtn.textContent = 'Connecting...';
                    msg.textContent = '';
                    try {
                        await API.sqwConnect({ username, password, target_user });
                        msg.textContent = 'Connected!';
                        msg.style.color = 'var(--success)';
                        setTimeout(() => this.renderSettings(), 1000);
                    } catch (err) {
                        let detail = err.message.replace(/^API \d+:\s*/, '');
                        try { detail = JSON.parse(detail).detail || detail; } catch {}
                        msg.textContent = detail;
                        msg.style.color = 'var(--danger)';
                        sqwConnectBtn.textContent = 'Connect';
                        sqwConnectBtn.disabled = false;
                    }
                });
            }

            // SQW: Disconnect
            const sqwDisconnectBtn = document.getElementById('sqw-disconnect-btn');
            if (sqwDisconnectBtn) {
                sqwDisconnectBtn.addEventListener('click', async () => {
                    if (!confirm('Disconnect SquidgeWorld? This clears saved credentials.')) return;
                    try {
                        await API.sqwDisconnect();
                        this.renderSettings();
                    } catch (err) {
                        alert('Failed: ' + err.message);
                    }
                });
            }

            // SQW: Poll Now / Full Resync — see _pollingTabPoll/_pollingTabResync
            const sqwPollBtn = document.getElementById('sqw-poll-btn');
            if (sqwPollBtn) {
                sqwPollBtn.addEventListener('click', () => this._pollingTabPoll({
                    btn: sqwPollBtn, msgId: 'sqw-msg', platform: 'sqw', apiMethod: 'triggerSQWPoll',
                }));
            }
            const sqwResyncBtn = document.getElementById('sqw-resync-btn');
            if (sqwResyncBtn) {
                sqwResyncBtn.addEventListener('click', () => this._pollingTabResync({
                    btn: sqwResyncBtn, msgId: 'sqw-msg', platform: 'sqw', apiMethod: 'fullSQWResync',
                }));
            }

            // SQW: Notifications toggle
            const sqwNotifToggle = document.getElementById('pref-sqw-notifications');
            if (sqwNotifToggle) {
                sqwNotifToggle.addEventListener('change', async (e) => {
                    try {
                        await API.savePreferences({ sqw_notifications_enabled: e.target.checked });
                    } catch (err) {
                        e.target.checked = !e.target.checked;
                        alert('Failed to save preference: ' + err.message);
                    }
                });
            }

            // AO3: Connect
            const ao3ConnectBtn = document.getElementById('ao3-connect-btn');
            if (ao3ConnectBtn) {
                ao3ConnectBtn.addEventListener('click', async () => {
                    const msg = document.getElementById('ao3-msg');
                    const username = document.getElementById('ao3-username').value.trim();
                    const password = document.getElementById('ao3-password').value;
                    const target_user = document.getElementById('ao3-target-user').value.trim();
                    const sessionCookieEl = document.getElementById('ao3-session-cookie');
                    const session_cookie = sessionCookieEl ? sessionCookieEl.value.trim() : '';
                    if (!target_user) {
                        msg.textContent = 'Target user required';
                        msg.style.color = 'var(--danger)';
                        return;
                    }
                    if (!session_cookie && (!username || !password)) {
                        msg.textContent = 'Provide session cookie OR username + password';
                        msg.style.color = 'var(--danger)';
                        return;
                    }
                    ao3ConnectBtn.disabled = true;
                    ao3ConnectBtn.textContent = 'Connecting...';
                    msg.textContent = '';
                    try {
                        await API.ao3Connect({ username, password, target_user, session_cookie });
                        msg.textContent = 'Connected!';
                        msg.style.color = 'var(--success)';
                        setTimeout(() => this.renderSettings(), 1000);
                    } catch (err) {
                        let detail = err.message.replace(/^API \d+:\s*/, '');
                        try { detail = JSON.parse(detail).detail || detail; } catch {}
                        msg.textContent = detail;
                        msg.style.color = 'var(--danger)';
                        ao3ConnectBtn.textContent = 'Connect';
                        ao3ConnectBtn.disabled = false;
                    }
                });
            }

            // AO3: Disconnect
            const ao3DisconnectBtn = document.getElementById('ao3-disconnect-btn');
            if (ao3DisconnectBtn) {
                ao3DisconnectBtn.addEventListener('click', async () => {
                    if (!confirm('Disconnect AO3? This clears saved credentials.')) return;
                    try {
                        await API.ao3Disconnect();
                        this.renderSettings();
                    } catch (err) {
                        alert('Failed: ' + err.message);
                    }
                });
            }

            // AO3: Poll Now / Full Resync — see _pollingTabPoll/_pollingTabResync
            const ao3PollBtn = document.getElementById('ao3-poll-btn');
            if (ao3PollBtn) {
                ao3PollBtn.addEventListener('click', () => this._pollingTabPoll({
                    btn: ao3PollBtn, msgId: 'ao3-msg', platform: 'ao3', apiMethod: 'triggerAO3Poll',
                }));
            }
            const ao3ResyncBtn = document.getElementById('ao3-resync-btn');
            if (ao3ResyncBtn) {
                ao3ResyncBtn.addEventListener('click', () => this._pollingTabResync({
                    btn: ao3ResyncBtn, msgId: 'ao3-msg', platform: 'ao3', apiMethod: 'fullAO3Resync',
                }));
            }

            // AO3: Notifications toggle
            const ao3NotifToggle = document.getElementById('pref-ao3-notifications');
            if (ao3NotifToggle) {
                ao3NotifToggle.addEventListener('change', async (e) => {
                    try {
                        await API.savePreferences({ ao3_notifications_enabled: e.target.checked });
                    } catch (err) {
                        e.target.checked = !e.target.checked;
                        alert('Failed to save preference: ' + err.message);
                    }
                });
            }

            // DA Connect: sends client_id + client_secret + target_user (official OAuth2 API)
            const daConnectBtn = document.getElementById('da-connect-btn');
            if (daConnectBtn) {
                daConnectBtn.addEventListener('click', async () => {
                    const msg = document.getElementById('da-msg');
                    const client_id = document.getElementById('da-client-id').value.trim();
                    const client_secret = document.getElementById('da-client-secret').value.trim();
                    const target_user = document.getElementById('da-target-user').value.trim();
                    if (!client_id || !client_secret || !target_user) {
                        msg.textContent = 'client_id, client_secret and username are all required';
                        msg.style.color = 'var(--danger)';
                        return;
                    }
                    daConnectBtn.disabled = true;
                    daConnectBtn.textContent = 'Connecting...';
                    msg.textContent = '';
                    try {
                        await API.daConnect({ client_id, client_secret, target_user });
                        msg.textContent = 'Connected!';
                        msg.style.color = 'var(--success)';
                        setTimeout(() => this.renderSettings(), 1000);
                    } catch (err) {
                        let detail = err.message.replace(/^API \d+:\s*/, '');
                        try { detail = JSON.parse(detail).detail || detail; } catch {}
                        msg.textContent = detail;
                        msg.style.color = 'var(--danger)';
                        daConnectBtn.textContent = 'Connect';
                        daConnectBtn.disabled = false;
                    }
                });
            }

            // DA posting authorisation. Distinct from Connect above: that mints an
            // app-only token for reading stats, this one asks DeviantArt for
            // permission to post AS the user, which only a browser approval can
            // grant. Posting was silently unconfigured until this existed —
            // Settings showed DA connected because it was, for polling.
            const daPostingStatusEl = document.getElementById('da-posting-status');
            if (daPostingStatusEl) {
                API.getDAPostingStatus().then(s => {
                    if (s.has_refresh_token) {
                        daPostingStatusEl.textContent = 'Authorised';
                        daPostingStatusEl.className = 'telegram-status connected';
                    } else {
                        daPostingStatusEl.textContent = 'Not authorised — posting will fail';
                        daPostingStatusEl.className = 'telegram-status disconnected';
                    }
                }).catch(() => {
                    daPostingStatusEl.textContent = 'Unknown';
                });
            }

            const daAuthorizeBtn = document.getElementById('da-authorize-btn');
            if (daAuthorizeBtn) {
                daAuthorizeBtn.addEventListener('click', async () => {
                    const msg = document.getElementById('da-authorize-msg');
                    msg.textContent = '';
                    daAuthorizeBtn.disabled = true;
                    try {
                        const info = await API.getDAAuthorizeUrl();
                        // Always show the redirect URI, not just on failure: DA's
                        // mismatch error appears on ITS page, not ours, so by the
                        // time it is wrong the user has already left.
                        const hint = document.getElementById('da-redirect-hint');
                        const uriEl = document.getElementById('da-redirect-uri');
                        if (hint && uriEl) {
                            uriEl.textContent = info.redirect_uri;
                            hint.style.display = 'block';
                        }
                        window.open(info.url, '_blank', 'noopener');
                        msg.textContent = 'Approve it in the new tab, then reopen this page.';
                        msg.style.color = 'var(--text-muted)';
                    } catch (err) {
                        let detail = err.message.replace(/^API \d+:\s*/, '');
                        try { detail = JSON.parse(detail).detail || detail; } catch {}
                        msg.textContent = detail;
                        msg.style.color = 'var(--danger)';
                    } finally {
                        daAuthorizeBtn.disabled = false;
                    }
                });
            }

            // DA Disconnect: clears saved credentials, re-renders to show connect form
            const daDisconnectBtn = document.getElementById('da-disconnect-btn');
            if (daDisconnectBtn) {
                daDisconnectBtn.addEventListener('click', async () => {
                    if (!confirm('Disconnect DeviantArt? This stops polling (your app client_id/secret stay saved for posting).')) return;
                    try {
                        await API.daDisconnect();
                        this.renderSettings();
                    } catch (err) {
                        alert('Failed: ' + err.message);
                    }
                });
            }

            // DA Poll Now / Full Resync — see _pollingTabPoll/_pollingTabResync
            const daPollBtn = document.getElementById('da-poll-btn');
            if (daPollBtn) {
                daPollBtn.addEventListener('click', () => this._pollingTabPoll({
                    btn: daPollBtn, msgId: 'da-msg', platform: 'da', apiMethod: 'triggerDAPoll',
                }));
            }
            const daResyncBtn = document.getElementById('da-resync-btn');
            if (daResyncBtn) {
                daResyncBtn.addEventListener('click', () => this._pollingTabResync({
                    btn: daResyncBtn, msgId: 'da-msg', platform: 'da', apiMethod: 'fullDAResync',
                }));
            }

            // DA: Notifications toggle
            const daNotifToggle = document.getElementById('pref-da-notifications');
            if (daNotifToggle) {
                daNotifToggle.addEventListener('change', async (e) => {
                    try {
                        await API.savePreferences({ da_notifications_enabled: e.target.checked });
                    } catch (err) {
                        e.target.checked = !e.target.checked;
                        alert('Failed to save preference: ' + err.message);
                    }
                });
            }

            // WP Connect: sends just target_user (username-only auth, no password/cookie)
            const wpConnectBtn = document.getElementById('wp-connect-btn');
            if (wpConnectBtn) {
                wpConnectBtn.addEventListener('click', async () => {
                    const msg = document.getElementById('wp-msg');
                    const target_user = document.getElementById('wp-target-user').value.trim();
                    if (!target_user) {
                        msg.textContent = 'Username is required';
                        msg.style.color = 'var(--danger)';
                        return;
                    }
                    wpConnectBtn.disabled = true;
                    wpConnectBtn.textContent = 'Connecting...';
                    msg.textContent = '';
                    try {
                        await API.wpConnect({ target_user });
                        msg.textContent = 'Connected!';
                        msg.style.color = 'var(--success)';
                        setTimeout(() => this.renderSettings(), 1000);
                    } catch (err) {
                        let detail = err.message.replace(/^API \d+:\s*/, '');
                        try { detail = JSON.parse(detail).detail || detail; } catch {}
                        msg.textContent = detail;
                        msg.style.color = 'var(--danger)';
                        wpConnectBtn.textContent = 'Connect';
                        wpConnectBtn.disabled = false;
                    }
                });
            }

            // WP Disconnect: clears saved credentials, re-renders to show connect form
            const wpDisconnectBtn = document.getElementById('wp-disconnect-btn');
            if (wpDisconnectBtn) {
                wpDisconnectBtn.addEventListener('click', async () => {
                    if (!confirm('Disconnect Wattpad? This clears the tracked username.')) return;
                    try {
                        await API.wpDisconnect();
                        this.renderSettings();
                    } catch (err) {
                        alert('Failed: ' + err.message);
                    }
                });
            }

            // WP Poll Now / Full Resync — see _pollingTabPoll/_pollingTabResync
            const wpPollBtn = document.getElementById('wp-poll-btn');
            if (wpPollBtn) {
                wpPollBtn.addEventListener('click', () => this._pollingTabPoll({
                    btn: wpPollBtn, msgId: 'wp-msg', platform: 'wp', apiMethod: 'triggerWPPoll',
                }));
            }
            const wpResyncBtn = document.getElementById('wp-resync-btn');
            if (wpResyncBtn) {
                wpResyncBtn.addEventListener('click', () => this._pollingTabResync({
                    btn: wpResyncBtn, msgId: 'wp-msg', platform: 'wp', apiMethod: 'fullWPResync',
                }));
            }

            // WP: Notifications toggle
            const wpNotifToggle = document.getElementById('pref-wp-notifications');
            if (wpNotifToggle) {
                wpNotifToggle.addEventListener('change', async (e) => {
                    try {
                        await API.savePreferences({ wp_notifications_enabled: e.target.checked });
                    } catch (err) {
                        e.target.checked = !e.target.checked;
                        alert('Failed to save preference: ' + err.message);
                    }
                });
            }

            // IK Connect: sends just target_user (username-only auth, no password/cookie)
            const ikConnectBtn = document.getElementById('ik-connect-btn');
            if (ikConnectBtn) {
                ikConnectBtn.addEventListener('click', async () => {
                    const msg = document.getElementById('ik-msg');
                    const target_user = document.getElementById('ik-target-user').value.trim();
                    const auth_token = (document.getElementById('ik-auth-token')?.value || '').trim();
                    if (!target_user) {
                        msg.textContent = 'Username is required';
                        msg.style.color = 'var(--danger)';
                        return;
                    }
                    ikConnectBtn.disabled = true;
                    ikConnectBtn.textContent = 'Connecting...';
                    msg.textContent = '';
                    try {
                        await API.ikConnect({ target_user, auth_token });
                        msg.textContent = 'Connected!';
                        msg.style.color = 'var(--success)';
                        setTimeout(() => this.renderSettings(), 1000);
                    } catch (err) {
                        let detail = err.message.replace(/^API \d+:\s*/, '');
                        try { detail = JSON.parse(detail).detail || detail; } catch {}
                        msg.textContent = detail;
                        msg.style.color = 'var(--danger)';
                        ikConnectBtn.textContent = 'Connect';
                        ikConnectBtn.disabled = false;
                    }
                });
            }

            // IK Save token: set/replace the auth token on an already-connected
            // account (enables posting) without re-validating the username.
            const ikSaveTokenBtn = document.getElementById('ik-savetoken-btn');
            if (ikSaveTokenBtn) {
                ikSaveTokenBtn.addEventListener('click', async () => {
                    const tmsg = document.getElementById('ik-token-msg');
                    const token = (document.getElementById('ik-auth-token')?.value || '').trim();
                    if (!token) { if (tmsg) { tmsg.textContent = 'Paste a token first'; tmsg.style.color = 'var(--danger)'; } return; }
                    ikSaveTokenBtn.disabled = true;
                    try {
                        await API.ikSetToken(token);
                        if (tmsg) { tmsg.textContent = 'Saved — posting enabled'; tmsg.style.color = 'var(--success)'; }
                        setTimeout(() => this.renderSettings(), 900);
                    } catch (err) {
                        let detail = err.message.replace(/^API \d+:\s*/, '');
                        try { detail = JSON.parse(detail).detail || detail; } catch {}
                        if (tmsg) { tmsg.textContent = detail; tmsg.style.color = 'var(--danger)'; }
                        ikSaveTokenBtn.disabled = false;
                    }
                });
            }

            // IK Disconnect: clears saved credentials, re-renders to show connect form
            const ikDisconnectBtn = document.getElementById('ik-disconnect-btn');
            if (ikDisconnectBtn) {
                ikDisconnectBtn.addEventListener('click', async () => {
                    if (!confirm('Disconnect Itaku? This clears the tracked username.')) return;
                    try {
                        await API.ikDisconnect();
                        this.renderSettings();
                    } catch (err) {
                        alert('Failed: ' + err.message);
                    }
                });
            }

            // IK Poll Now / Full Resync — see _pollingTabPoll/_pollingTabResync
            const ikPollBtn = document.getElementById('ik-poll-btn');
            if (ikPollBtn) {
                ikPollBtn.addEventListener('click', () => this._pollingTabPoll({
                    btn: ikPollBtn, msgId: 'ik-msg', platform: 'ik', apiMethod: 'triggerIKPoll',
                }));
            }
            const ikResyncBtn = document.getElementById('ik-resync-btn');
            if (ikResyncBtn) {
                ikResyncBtn.addEventListener('click', () => this._pollingTabResync({
                    btn: ikResyncBtn, msgId: 'ik-msg', platform: 'ik', apiMethod: 'fullIKResync',
                }));
            }

            // IK: Notifications toggle
            const ikNotifToggle = document.getElementById('pref-ik-notifications');
            if (ikNotifToggle) {
                ikNotifToggle.addEventListener('change', async (e) => {
                    try {
                        await API.savePreferences({ ik_notifications_enabled: e.target.checked });
                    } catch (err) {
                        e.target.checked = !e.target.checked;
                        alert('Failed to save preference: ' + err.message);
                    }
                });
            }

            // BSKY Connect: sends identifier + app_password
            const bskyConnectBtn = document.getElementById('bsky-connect-btn');
            if (bskyConnectBtn) {
                bskyConnectBtn.addEventListener('click', async () => {
                    const msg = document.getElementById('bsky-msg');
                    const identifier = document.getElementById('bsky-identifier').value.trim();
                    const app_password = document.getElementById('bsky-app-password').value.trim();
                    if (!identifier || !app_password) {
                        msg.textContent = 'Handle and app password are required';
                        msg.style.color = 'var(--danger)';
                        return;
                    }
                    bskyConnectBtn.disabled = true;
                    bskyConnectBtn.textContent = 'Connecting...';
                    msg.textContent = '';
                    try {
                        await API.bskyConnect({ identifier, app_password });
                        msg.textContent = 'Connected!';
                        msg.style.color = 'var(--success)';
                        setTimeout(() => this.renderSettings(), 1000);
                    } catch (err) {
                        let detail = err.message.replace(/^API \d+:\s*/, '');
                        try { detail = JSON.parse(detail).detail || detail; } catch {}
                        msg.textContent = detail;
                        msg.style.color = 'var(--danger)';
                        bskyConnectBtn.textContent = 'Connect';
                        bskyConnectBtn.disabled = false;
                    }
                });
            }

            // BSKY Disconnect
            const bskyDisconnectBtn = document.getElementById('bsky-disconnect-btn');
            if (bskyDisconnectBtn) {
                bskyDisconnectBtn.addEventListener('click', async () => {
                    if (!confirm('Disconnect Bluesky? This clears your credentials.')) return;
                    try {
                        await API.bskyDisconnect();
                        this.renderSettings();
                    } catch (err) {
                        alert('Failed: ' + err.message);
                    }
                });
            }

            // BSKY Poll Now / Full Resync — see _pollingTabPoll/_pollingTabResync
            const bskyPollBtn = document.getElementById('bsky-poll-btn');
            if (bskyPollBtn) {
                bskyPollBtn.addEventListener('click', () => this._pollingTabPoll({
                    btn: bskyPollBtn, msgId: 'bsky-msg', platform: 'bsky', apiMethod: 'triggerBSKYPoll',
                }));
            }
            const bskyResyncBtn = document.getElementById('bsky-resync-btn');
            if (bskyResyncBtn) {
                bskyResyncBtn.addEventListener('click', () => this._pollingTabResync({
                    btn: bskyResyncBtn, msgId: 'bsky-msg', platform: 'bsky', apiMethod: 'fullBSKYResync',
                }));
            }

            // BSKY: Notifications toggle
            const bskyNotifToggle = document.getElementById('pref-bsky-notifications');
            if (bskyNotifToggle) {
                bskyNotifToggle.addEventListener('change', async (e) => {
                    try {
                        await API.savePreferences({ bsky_notifications_enabled: e.target.checked });
                    } catch (err) {
                        e.target.checked = !e.target.checked;
                        alert('Failed to save preference: ' + err.message);
                    }
                });
            }

            // MAST Connect: sends instance_url + access_token
            const mastConnectBtn = document.getElementById('mast-connect-btn');
            if (mastConnectBtn) {
                mastConnectBtn.addEventListener('click', async () => {
                    const msg = document.getElementById('mast-msg');
                    const instance_url = document.getElementById('mast-instance-url').value.trim();
                    const access_token = document.getElementById('mast-access-token').value.trim();
                    if (!instance_url || !access_token) {
                        msg.textContent = 'Instance URL and access token are required';
                        msg.style.color = 'var(--danger)';
                        return;
                    }
                    mastConnectBtn.disabled = true;
                    mastConnectBtn.textContent = 'Connecting...';
                    msg.textContent = '';
                    try {
                        await API.mastConnect({ instance_url, access_token });
                        msg.textContent = 'Connected!';
                        msg.style.color = 'var(--success)';
                        setTimeout(() => this.renderSettings(), 1000);
                    } catch (err) {
                        let detail = err.message.replace(/^API \d+:\s*/, '');
                        try { detail = JSON.parse(detail).detail || detail; } catch {}
                        msg.textContent = detail;
                        msg.style.color = 'var(--danger)';
                        mastConnectBtn.textContent = 'Connect';
                        mastConnectBtn.disabled = false;
                    }
                });
            }

            // MAST Disconnect
            const mastDisconnectBtn = document.getElementById('mast-disconnect-btn');
            if (mastDisconnectBtn) {
                mastDisconnectBtn.addEventListener('click', async () => {
                    if (!confirm('Disconnect Mastodon? This clears your credentials.')) return;
                    try {
                        await API.mastDisconnect();
                        this.renderSettings();
                    } catch (err) {
                        alert('Failed: ' + err.message);
                    }
                });
            }

            // MAST Poll Now / Full Resync — see _pollingTabPoll/_pollingTabResync
            const mastPollBtn = document.getElementById('mast-poll-btn');
            if (mastPollBtn) {
                mastPollBtn.addEventListener('click', () => this._pollingTabPoll({
                    btn: mastPollBtn, msgId: 'mast-msg', platform: 'mast', apiMethod: 'triggerMASTPoll',
                }));
            }
            const mastResyncBtn = document.getElementById('mast-resync-btn');
            if (mastResyncBtn) {
                mastResyncBtn.addEventListener('click', () => this._pollingTabResync({
                    btn: mastResyncBtn, msgId: 'mast-msg', platform: 'mast', apiMethod: 'fullMASTResync',
                }));
            }

            // MAST: Notifications toggle
            const mastNotifToggle = document.getElementById('pref-mast-notifications');
            if (mastNotifToggle) {
                mastNotifToggle.addEventListener('change', async (e) => {
                    try {
                        await API.savePreferences({ mast_notifications_enabled: e.target.checked });
                    } catch (err) {
                        e.target.checked = !e.target.checked;
                        alert('Failed to save preference: ' + err.message);
                    }
                });
            }

            // TUM Connect: sends api_key + blog
            const tumConnectBtn = document.getElementById('tum-connect-btn');
            if (tumConnectBtn) {
                tumConnectBtn.addEventListener('click', async () => {
                    const msg = document.getElementById('tum-msg');
                    const blog = document.getElementById('tum-blog').value.trim();
                    const api_key = document.getElementById('tum-api-key').value.trim();
                    if (!blog || !api_key) {
                        msg.textContent = 'Blog and API key are required';
                        msg.style.color = 'var(--danger)';
                        return;
                    }
                    tumConnectBtn.disabled = true;
                    tumConnectBtn.textContent = 'Connecting...';
                    msg.textContent = '';
                    try {
                        await API.tumConnect({ blog, api_key });
                        msg.textContent = 'Connected!';
                        msg.style.color = 'var(--success)';
                        setTimeout(() => this.renderSettings(), 1000);
                    } catch (err) {
                        let detail = err.message.replace(/^API \d+:\s*/, '');
                        try { detail = JSON.parse(detail).detail || detail; } catch {}
                        msg.textContent = detail;
                        msg.style.color = 'var(--danger)';
                        tumConnectBtn.textContent = 'Connect';
                        tumConnectBtn.disabled = false;
                    }
                });
            }

            // TUM Disconnect
            const tumDisconnectBtn = document.getElementById('tum-disconnect-btn');
            if (tumDisconnectBtn) {
                tumDisconnectBtn.addEventListener('click', async () => {
                    if (!confirm('Disconnect Tumblr? This clears your credentials.')) return;
                    try {
                        await API.tumDisconnect();
                        this.renderSettings();
                    } catch (err) {
                        alert('Failed: ' + err.message);
                    }
                });
            }

            // TUM Poll Now / Full Resync
            const tumPollBtn = document.getElementById('tum-poll-btn');
            if (tumPollBtn) {
                tumPollBtn.addEventListener('click', () => this._pollingTabPoll({
                    btn: tumPollBtn, msgId: 'tum-msg', platform: 'tum', apiMethod: 'triggerTUMPoll',
                }));
            }
            const tumResyncBtn = document.getElementById('tum-resync-btn');
            if (tumResyncBtn) {
                tumResyncBtn.addEventListener('click', () => this._pollingTabResync({
                    btn: tumResyncBtn, msgId: 'tum-msg', platform: 'tum', apiMethod: 'fullTUMResync',
                }));
            }

            // TUM: Notifications toggle
            const tumNotifToggle = document.getElementById('pref-tum-notifications');
            if (tumNotifToggle) {
                tumNotifToggle.addEventListener('change', async (e) => {
                    try {
                        await API.savePreferences({ tum_notifications_enabled: e.target.checked });
                    } catch (err) {
                        e.target.checked = !e.target.checked;
                        alert('Failed to save preference: ' + err.message);
                    }
                });
            }

            // PIX Connect: sends refresh_token + optional user_id
            const pixConnectBtn = document.getElementById('pix-connect-btn');
            if (pixConnectBtn) {
                pixConnectBtn.addEventListener('click', async () => {
                    const msg = document.getElementById('pix-msg');
                    const refresh_token = document.getElementById('pix-refresh-token').value.trim();
                    const user_id = document.getElementById('pix-user-id').value.trim();
                    if (!refresh_token) {
                        msg.textContent = 'Refresh token is required';
                        msg.style.color = 'var(--danger)';
                        return;
                    }
                    pixConnectBtn.disabled = true;
                    pixConnectBtn.textContent = 'Connecting...';
                    msg.textContent = '';
                    try {
                        await API.pixConnect({ refresh_token, user_id });
                        msg.textContent = 'Connected!';
                        msg.style.color = 'var(--success)';
                        setTimeout(() => this.renderSettings(), 1000);
                    } catch (err) {
                        let detail = err.message.replace(/^API \d+:\s*/, '');
                        try { detail = JSON.parse(detail).detail || detail; } catch {}
                        msg.textContent = detail;
                        msg.style.color = 'var(--danger)';
                        pixConnectBtn.textContent = 'Connect';
                        pixConnectBtn.disabled = false;
                    }
                });
            }

            // PIX Disconnect
            const pixDisconnectBtn = document.getElementById('pix-disconnect-btn');
            if (pixDisconnectBtn) {
                pixDisconnectBtn.addEventListener('click', async () => {
                    if (!confirm('Disconnect Pixiv? This clears your credentials.')) return;
                    try {
                        await API.pixDisconnect();
                        this.renderSettings();
                    } catch (err) {
                        alert('Failed: ' + err.message);
                    }
                });
            }

            // PIX Poll Now / Full Resync
            const pixPollBtn = document.getElementById('pix-poll-btn');
            if (pixPollBtn) {
                pixPollBtn.addEventListener('click', () => this._pollingTabPoll({
                    btn: pixPollBtn, msgId: 'pix-msg', platform: 'pix', apiMethod: 'triggerPIXPoll',
                }));
            }
            const pixResyncBtn = document.getElementById('pix-resync-btn');
            if (pixResyncBtn) {
                pixResyncBtn.addEventListener('click', () => this._pollingTabResync({
                    btn: pixResyncBtn, msgId: 'pix-msg', platform: 'pix', apiMethod: 'fullPIXResync',
                }));
            }

            // PIX: Notifications toggle
            const pixNotifToggle = document.getElementById('pref-pix-notifications');
            if (pixNotifToggle) {
                pixNotifToggle.addEventListener('change', async (e) => {
                    try {
                        await API.savePreferences({ pix_notifications_enabled: e.target.checked });
                    } catch (err) {
                        e.target.checked = !e.target.checked;
                        alert('Failed to save preference: ' + err.message);
                    }
                });
            }

            // THR Connect: sends access_token + optional user_id
            const thrConnectBtn = document.getElementById('thr-connect-btn');
            if (thrConnectBtn) {
                thrConnectBtn.addEventListener('click', async () => {
                    const msg = document.getElementById('thr-msg');
                    const access_token = document.getElementById('thr-access-token').value.trim();
                    const user_id = document.getElementById('thr-user-id').value.trim();
                    if (!access_token) {
                        msg.textContent = 'Access token is required';
                        msg.style.color = 'var(--danger)';
                        return;
                    }
                    thrConnectBtn.disabled = true;
                    thrConnectBtn.textContent = 'Connecting...';
                    msg.textContent = '';
                    try {
                        await API.thrConnect({ access_token, user_id });
                        msg.textContent = 'Connected!';
                        msg.style.color = 'var(--success)';
                        setTimeout(() => this.renderSettings(), 1000);
                    } catch (err) {
                        let detail = err.message.replace(/^API \d+:\s*/, '');
                        try { detail = JSON.parse(detail).detail || detail; } catch {}
                        msg.textContent = detail;
                        msg.style.color = 'var(--danger)';
                        thrConnectBtn.textContent = 'Connect';
                        thrConnectBtn.disabled = false;
                    }
                });
            }

            // THR Disconnect
            const thrDisconnectBtn = document.getElementById('thr-disconnect-btn');
            if (thrDisconnectBtn) {
                thrDisconnectBtn.addEventListener('click', async () => {
                    if (!confirm('Disconnect Threads? This clears your credentials.')) return;
                    try {
                        await API.thrDisconnect();
                        this.renderSettings();
                    } catch (err) {
                        alert('Failed: ' + err.message);
                    }
                });
            }

            // THR Poll Now / Full Resync
            const thrPollBtn = document.getElementById('thr-poll-btn');
            if (thrPollBtn) {
                thrPollBtn.addEventListener('click', () => this._pollingTabPoll({
                    btn: thrPollBtn, msgId: 'thr-msg', platform: 'thr', apiMethod: 'triggerTHRPoll',
                }));
            }
            const thrResyncBtn = document.getElementById('thr-resync-btn');
            if (thrResyncBtn) {
                thrResyncBtn.addEventListener('click', () => this._pollingTabResync({
                    btn: thrResyncBtn, msgId: 'thr-msg', platform: 'thr', apiMethod: 'fullTHRResync',
                }));
            }

            // THR: Notifications toggle
            const thrNotifToggle = document.getElementById('pref-thr-notifications');
            if (thrNotifToggle) {
                thrNotifToggle.addEventListener('change', async (e) => {
                    try {
                        await API.savePreferences({ thr_notifications_enabled: e.target.checked });
                    } catch (err) {
                        e.target.checked = !e.target.checked;
                        alert('Failed to save preference: ' + err.message);
                    }
                });
            }

            // IG Connect: sends access_token + optional user_id
            const igConnectBtn = document.getElementById('ig-connect-btn');
            if (igConnectBtn) {
                igConnectBtn.addEventListener('click', async () => {
                    const msg = document.getElementById('ig-msg');
                    const access_token = document.getElementById('ig-access-token').value.trim();
                    const user_id = document.getElementById('ig-user-id').value.trim();
                    if (!access_token) {
                        msg.textContent = 'Access token is required';
                        msg.style.color = 'var(--danger)';
                        return;
                    }
                    igConnectBtn.disabled = true;
                    igConnectBtn.textContent = 'Connecting...';
                    msg.textContent = '';
                    try {
                        await API.igConnect({ access_token, user_id });
                        msg.textContent = 'Connected!';
                        msg.style.color = 'var(--success)';
                        setTimeout(() => this.renderSettings(), 1000);
                    } catch (err) {
                        let detail = err.message.replace(/^API \d+:\s*/, '');
                        try { detail = JSON.parse(detail).detail || detail; } catch {}
                        msg.textContent = detail;
                        msg.style.color = 'var(--danger)';
                        igConnectBtn.textContent = 'Connect';
                        igConnectBtn.disabled = false;
                    }
                });
            }

            // IG Disconnect
            const igDisconnectBtn = document.getElementById('ig-disconnect-btn');
            if (igDisconnectBtn) {
                igDisconnectBtn.addEventListener('click', async () => {
                    if (!confirm('Disconnect Instagram? This clears your credentials.')) return;
                    try {
                        await API.igDisconnect();
                        this.renderSettings();
                    } catch (err) {
                        alert('Failed: ' + err.message);
                    }
                });
            }

            // IG Poll Now / Full Resync
            const igPollBtn = document.getElementById('ig-poll-btn');
            if (igPollBtn) {
                igPollBtn.addEventListener('click', () => this._pollingTabPoll({
                    btn: igPollBtn, msgId: 'ig-msg', platform: 'ig', apiMethod: 'triggerIGPoll',
                }));
            }
            const igResyncBtn = document.getElementById('ig-resync-btn');
            if (igResyncBtn) {
                igResyncBtn.addEventListener('click', () => this._pollingTabResync({
                    btn: igResyncBtn, msgId: 'ig-msg', platform: 'ig', apiMethod: 'fullIGResync',
                }));
            }

            // IG: Notifications toggle
            const igNotifToggle = document.getElementById('pref-ig-notifications');
            if (igNotifToggle) {
                igNotifToggle.addEventListener('change', async (e) => {
                    try {
                        await API.savePreferences({ ig_notifications_enabled: e.target.checked });
                    } catch (err) {
                        e.target.checked = !e.target.checked;
                        alert('Failed to save preference: ' + err.message);
                    }
                });
            }

            // TW Connect: sends auth_token + ct0 + target_user
            const twConnectBtn = document.getElementById('tw-connect-btn');
            if (twConnectBtn) {
                twConnectBtn.addEventListener('click', async () => {
                    const msg = document.getElementById('tw-msg');
                    const auth_token = document.getElementById('tw-auth-token').value.trim();
                    const ct0 = document.getElementById('tw-ct0').value.trim();
                    const target_user = document.getElementById('tw-target-user').value.trim();
                    if (!auth_token || !target_user) {
                        msg.textContent = 'auth_token and username are required';
                        msg.style.color = 'var(--danger)';
                        return;
                    }
                    twConnectBtn.disabled = true;
                    twConnectBtn.textContent = 'Connecting...';
                    msg.textContent = '';
                    try {
                        await API.twConnect({ auth_token, ct0, target_user });
                        msg.textContent = 'Connected!';
                        msg.style.color = 'var(--success)';
                        setTimeout(() => this.renderSettings(), 1000);
                    } catch (err) {
                        let detail = err.message.replace(/^API \d+:\s*/, '');
                        try { detail = JSON.parse(detail).detail || detail; } catch {}
                        msg.textContent = detail;
                        msg.style.color = 'var(--danger)';
                        twConnectBtn.textContent = 'Connect';
                        twConnectBtn.disabled = false;
                    }
                });
            }

            // TW Disconnect
            const twDisconnectBtn = document.getElementById('tw-disconnect-btn');
            if (twDisconnectBtn) {
                twDisconnectBtn.addEventListener('click', async () => {
                    if (!confirm('Disconnect X/Twitter? This clears your cookies.')) return;
                    try {
                        await API.twDisconnect();
                        this.renderSettings();
                    } catch (err) {
                        alert('Failed: ' + err.message);
                    }
                });
            }

            // TW: Official X API token — save / remove (opt-in official poll backend)
            const twApiSaveBtn = document.getElementById('tw-apitoken-save-btn');
            if (twApiSaveBtn) {
                twApiSaveBtn.addEventListener('click', async () => {
                    const bearer = (document.getElementById('tw-api-bearer')?.value || '').trim();
                    const target = (document.getElementById('tw-api-target')?.value || '').trim();
                    const msg = document.getElementById('tw-apitoken-msg');
                    if (!bearer) { if (msg) msg.textContent = 'Enter a Bearer token'; return; }
                    twApiSaveBtn.disabled = true;
                    twApiSaveBtn.textContent = 'Validating...';
                    try {
                        const r = await API.twApiTokenConnect({ bearer_token: bearer, target_user: target });
                        if (msg) msg.textContent = r.message || 'Saved';
                        this.renderSettings();
                    } catch (err) {
                        if (msg) msg.textContent = 'Failed: ' + err.message;
                        twApiSaveBtn.textContent = 'Save API token';
                        twApiSaveBtn.disabled = false;
                    }
                });
            }
            const twApiRemoveBtn = document.getElementById('tw-apitoken-remove-btn');
            if (twApiRemoveBtn) {
                twApiRemoveBtn.addEventListener('click', async () => {
                    if (!confirm('Remove the X API token? Polling falls back to the free gallery-dl / scrape backend.')) return;
                    try {
                        await API.twApiTokenDisconnect();
                        this.renderSettings();
                    } catch (err) {
                        alert('Failed: ' + err.message);
                    }
                });
            }

            // TW Poll Now / Full Resync — see _pollingTabPoll/_pollingTabResync
            const twPollBtn = document.getElementById('tw-poll-btn');
            if (twPollBtn) {
                twPollBtn.addEventListener('click', () => this._pollingTabPoll({
                    btn: twPollBtn, msgId: 'tw-msg', platform: 'tw', apiMethod: 'triggerTWPoll',
                }));
            }
            const twResyncBtn = document.getElementById('tw-resync-btn');
            if (twResyncBtn) {
                twResyncBtn.addEventListener('click', () => this._pollingTabResync({
                    btn: twResyncBtn, msgId: 'tw-msg', platform: 'tw', apiMethod: 'fullTWResync',
                }));
            }

            // TW: Notifications toggle
            const twNotifToggle = document.getElementById('pref-tw-notifications');
            if (twNotifToggle) {
                twNotifToggle.addEventListener('change', async (e) => {
                    try {
                        await API.savePreferences({ tw_notifications_enabled: e.target.checked });
                    } catch (err) {
                        e.target.checked = !e.target.checked;
                        alert('Failed to save preference: ' + err.message);
                    }
                });
            }

            // e621 Connect: sends username + api_key
            const e621ConnectBtn = document.getElementById('e621-connect-btn');
            if (e621ConnectBtn) {
                e621ConnectBtn.addEventListener('click', async () => {
                    const msg = document.getElementById('e621-msg');
                    const username = document.getElementById('e621-username').value.trim();
                    const api_key = document.getElementById('e621-api-key').value.trim();
                    if (!username || !api_key) {
                        msg.textContent = 'Username and API key are required';
                        msg.style.color = 'var(--danger)';
                        return;
                    }
                    e621ConnectBtn.disabled = true;
                    e621ConnectBtn.textContent = 'Connecting...';
                    msg.textContent = '';
                    try {
                        await API.e621Connect({ username, api_key });
                        msg.textContent = 'Connected!';
                        msg.style.color = 'var(--success)';
                        setTimeout(() => this.renderSettings(), 1000);
                    } catch (err) {
                        let detail = err.message.replace(/^API \d+:\s*/, '');
                        try { detail = JSON.parse(detail).detail || detail; } catch {}
                        msg.textContent = detail;
                        msg.style.color = 'var(--danger)';
                        e621ConnectBtn.textContent = 'Connect';
                        e621ConnectBtn.disabled = false;
                    }
                });
            }

            // e621 Disconnect
            const e621DisconnectBtn = document.getElementById('e621-disconnect-btn');
            if (e621DisconnectBtn) {
                e621DisconnectBtn.addEventListener('click', async () => {
                    if (!confirm('Disconnect e621? This clears your credentials.')) return;
                    try {
                        await API.e621Disconnect();
                        this.renderSettings();
                    } catch (err) {
                        alert('Failed: ' + err.message);
                    }
                });
            }

            // e621 Poll Now / Full Resync
            const e621PollBtn = document.getElementById('e621-poll-btn');
            if (e621PollBtn) {
                e621PollBtn.addEventListener('click', () => this._pollingTabPoll({
                    btn: e621PollBtn, msgId: 'e621-msg', platform: 'e621', apiMethod: 'triggerE621Poll',
                }));
            }
            const e621ResyncBtn = document.getElementById('e621-resync-btn');
            if (e621ResyncBtn) {
                e621ResyncBtn.addEventListener('click', () => this._pollingTabResync({
                    btn: e621ResyncBtn, msgId: 'e621-msg', platform: 'e621', apiMethod: 'fullE621Resync',
                }));
            }

            // e621: Notifications toggle
            const e621NotifToggle = document.getElementById('pref-e621-notifications');
            if (e621NotifToggle) {
                e621NotifToggle.addEventListener('change', async (e) => {
                    try {
                        await API.savePreferences({ e621_notifications_enabled: e.target.checked });
                    } catch (err) {
                        e.target.checked = !e.target.checked;
                        alert('Failed to save preference: ' + err.message);
                    }
                });
            }

            // ── FurryNetwork connect / disconnect / poll / notifications ──
            const fnConnectBtn = document.getElementById('fn-connect-btn');
            if (fnConnectBtn) {
                fnConnectBtn.addEventListener('click', async () => {
                    const msg = document.getElementById('fn-msg');
                    const username = document.getElementById('fn-username').value.trim();
                    const password = document.getElementById('fn-password').value;
                    const refresh_token = (document.getElementById('fn-refresh-token')?.value || '').trim();
                    // A refresh token alone is enough, and is the only thing that
                    // works since FN put the password grant behind reCAPTCHA.
                    if (!refresh_token && !(username && password)) {
                        msg.textContent = 'Paste a refresh token (email + password no longer works)';
                        msg.style.color = 'var(--danger)';
                        return;
                    }
                    fnConnectBtn.disabled = true;
                    fnConnectBtn.textContent = 'Connecting...';
                    msg.textContent = '';
                    try {
                        await API.fnConnect({ username, password, refresh_token });
                        msg.textContent = 'Connected!';
                        msg.style.color = 'var(--success)';
                        setTimeout(() => this.renderSettings(), 1000);
                    } catch (err) {
                        let detail = err.message.replace(/^API \d+:\s*/, '');
                        try { detail = JSON.parse(detail).detail || detail; } catch {}
                        msg.textContent = detail;
                        msg.style.color = 'var(--danger)';
                        fnConnectBtn.textContent = 'Connect';
                        fnConnectBtn.disabled = false;
                    }
                });
            }
            const fnDisconnectBtn = document.getElementById('fn-disconnect-btn');
            if (fnDisconnectBtn) {
                fnDisconnectBtn.addEventListener('click', async () => {
                    if (!confirm('Disconnect FurryNetwork? This clears your credentials.')) return;
                    try {
                        await API.fnDisconnect();
                        this.renderSettings();
                    } catch (err) {
                        alert('Failed: ' + err.message);
                    }
                });
            }
            const fnPollBtn = document.getElementById('fn-poll-btn');
            if (fnPollBtn) {
                fnPollBtn.addEventListener('click', () => this._pollingTabPoll({
                    btn: fnPollBtn, msgId: 'fn-msg', platform: 'fn', apiMethod: 'triggerFNPoll',
                }));
            }
            const fnResyncBtn = document.getElementById('fn-resync-btn');
            if (fnResyncBtn) {
                fnResyncBtn.addEventListener('click', () => this._pollingTabResync({
                    btn: fnResyncBtn, msgId: 'fn-msg', platform: 'fn', apiMethod: 'fullFNResync',
                }));
            }
            const fnNotifToggle = document.getElementById('pref-fn-notifications');
            if (fnNotifToggle) {
                fnNotifToggle.addEventListener('change', async (e) => {
                    try {
                        await API.savePreferences({ fn_notifications_enabled: e.target.checked });
                    } catch (err) {
                        e.target.checked = !e.target.checked;
                        alert('Failed to save preference: ' + err.message);
                    }
                });
            }

            // ── Furbooru connect / disconnect / poll / notifications ──
            // Connect sends username (required) + api_key (optional — the
            // Philomena read API is public; a key only raises the rate cap).
            const fbrConnectBtn = document.getElementById('fbr-connect-btn');
            if (fbrConnectBtn) {
                fbrConnectBtn.addEventListener('click', async () => {
                    const msg = document.getElementById('fbr-msg');
                    const username = document.getElementById('fbr-username').value.trim();
                    const api_key = document.getElementById('fbr-api-key').value.trim();
                    if (!username) {
                        msg.textContent = 'Username is required';
                        msg.style.color = 'var(--danger)';
                        return;
                    }
                    fbrConnectBtn.disabled = true;
                    fbrConnectBtn.textContent = 'Connecting...';
                    msg.textContent = '';
                    try {
                        await API.fbrConnect({ username, api_key });
                        msg.textContent = 'Connected!';
                        msg.style.color = 'var(--success)';
                        setTimeout(() => this.renderSettings(), 1000);
                    } catch (err) {
                        let detail = err.message.replace(/^API \d+:\s*/, '');
                        try { detail = JSON.parse(detail).detail || detail; } catch {}
                        msg.textContent = detail;
                        msg.style.color = 'var(--danger)';
                        fbrConnectBtn.textContent = 'Connect';
                        fbrConnectBtn.disabled = false;
                    }
                });
            }
            const fbrDisconnectBtn = document.getElementById('fbr-disconnect-btn');
            if (fbrDisconnectBtn) {
                fbrDisconnectBtn.addEventListener('click', async () => {
                    if (!confirm('Disconnect Furbooru? This clears your credentials.')) return;
                    try {
                        await API.fbrDisconnect();
                        this.renderSettings();
                    } catch (err) {
                        alert('Failed: ' + err.message);
                    }
                });
            }
            const fbrPollBtn = document.getElementById('fbr-poll-btn');
            if (fbrPollBtn) {
                fbrPollBtn.addEventListener('click', () => this._pollingTabPoll({
                    btn: fbrPollBtn, msgId: 'fbr-msg', platform: 'fbr', apiMethod: 'triggerFBRPoll',
                }));
            }
            const fbrResyncBtn = document.getElementById('fbr-resync-btn');
            if (fbrResyncBtn) {
                fbrResyncBtn.addEventListener('click', () => this._pollingTabResync({
                    btn: fbrResyncBtn, msgId: 'fbr-msg', platform: 'fbr', apiMethod: 'fullFBRResync',
                }));
            }
            const fbrNotifToggle = document.getElementById('pref-fbr-notifications');
            if (fbrNotifToggle) {
                fbrNotifToggle.addEventListener('change', async (e) => {
                    try {
                        await API.savePreferences({ fbr_notifications_enabled: e.target.checked });
                    } catch (err) {
                        e.target.checked = !e.target.checked;
                        alert('Failed to save preference: ' + err.message);
                    }
                });
            }

            // Danger zone — Uninstall PawPoller
            document.getElementById('uninstall-btn')?.addEventListener('click', () => {
                this._showUninstallDialog();
            });

            // Full backup (db + settings + logins + media). Show its size, and
            // download via a same-origin navigation so the session cookie rides.
            API.getBackupInfo().then(info => {
                const el = document.getElementById('backup-size');
                if (el && info && info.total_bytes) {
                    const mb = info.total_bytes / (1024 * 1024);
                    el.textContent = `Current size: ~${mb < 1 ? (info.total_bytes / 1024).toFixed(0) + ' KB' : mb.toFixed(1) + ' MB'}.`;
                }
            }).catch(() => { /* size is a nicety */ });
            document.getElementById('backup-download-btn')?.addEventListener('click', () => {
                window.location.href = '/api/backup/export';
            });

            // Auto-backup (gap G7) — load current config, wire enable / save / run-now.
            const _autobkFill = (s) => {
                if (!s) return;
                const en = document.getElementById('autobk-enabled'); if (en) en.checked = !!s.enabled;
                const iv = document.getElementById('autobk-interval'); if (iv) iv.value = s.interval_hours || 24;
                const kp = document.getElementById('autobk-keep'); if (kp) kp.value = s.keep || 7;
                const dir = document.getElementById('autobk-dir'); if (dir) dir.textContent = s.dir || '';
                const last = document.getElementById('autobk-last');
                if (last) last.textContent = s.last_at ? ('Last run: ' + new Date(s.last_at).toLocaleString()) : 'No auto-backup yet.';
            };
            fetch('/api/backup/auto').then(r => r.json()).then(_autobkFill).catch(() => {});
            const _autobkSave = async (runNow) => {
                const msg = document.getElementById('autobk-msg');
                const body = {
                    enabled: !!document.getElementById('autobk-enabled')?.checked,
                    interval_hours: parseInt(document.getElementById('autobk-interval')?.value, 10) || 24,
                    keep: parseInt(document.getElementById('autobk-keep')?.value, 10) || 7,
                    run_now: !!runNow,
                };
                if (msg) { msg.textContent = runNow ? 'Backing up…' : 'Saving…'; msg.style.color = 'var(--text-muted)'; }
                try {
                    const res = await fetch('/api/backup/auto', {
                        method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
                    }).then(r => r.json());
                    _autobkFill(res);
                    if (msg) {
                        msg.style.color = 'var(--success)';
                        msg.textContent = runNow
                            ? ('Backed up' + (res.ran ? ` (${(res.ran.bytes / 1048576).toFixed(1)} MB)` : '') + '.')
                            : (body.enabled ? 'Automatic backups on.' : 'Saved.');
                    }
                } catch (err) {
                    if (msg) { msg.style.color = 'var(--danger)'; msg.textContent = 'Error: ' + (err.message || err); }
                }
            };
            document.getElementById('autobk-save')?.addEventListener('click', () => _autobkSave(false));
            document.getElementById('autobk-now')?.addEventListener('click', () => _autobkSave(true));
            document.getElementById('autobk-enabled')?.addEventListener('change', () => _autobkSave(false));

            // Discord announcements (gap G4) — webhook + auto-toggle + test.
            const _discordFill = (s) => {
                if (!s) return;
                const auto = document.getElementById('discord-auto'); if (auto) auto.checked = !!s.announce_on_publish;
                const hint = document.getElementById('discord-hint');
                if (hint) hint.textContent = s.configured
                    ? ('Configured: ' + (s.webhook_hint || '') + ' — leave blank to keep it.')
                    : 'Not configured yet.';
            };
            fetch('/api/discord').then(r => r.json()).then(_discordFill).catch(() => {});
            const _discordSave = async () => {
                const msg = document.getElementById('discord-msg');
                const wh = document.getElementById('discord-webhook')?.value.trim();
                const body = { announce_on_publish: !!document.getElementById('discord-auto')?.checked };
                if (wh) body.webhook_url = wh;
                if (msg) { msg.textContent = 'Saving…'; msg.style.color = 'var(--text-muted)'; }
                try {
                    const res = await fetch('/api/discord', {
                        method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
                    }).then(async r => { if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || 'failed'); return r.json(); });
                    _discordFill(res);
                    const el = document.getElementById('discord-webhook'); if (el) el.value = '';
                    if (msg) { msg.style.color = 'var(--success)'; msg.textContent = 'Saved.'; }
                    return true;
                } catch (err) {
                    if (msg) { msg.style.color = 'var(--danger)'; msg.textContent = 'Error: ' + (err.message || err); }
                    return false;
                }
            };
            document.getElementById('discord-save')?.addEventListener('click', _discordSave);
            document.getElementById('discord-auto')?.addEventListener('change', _discordSave);
            document.getElementById('discord-test')?.addEventListener('click', async () => {
                const msg = document.getElementById('discord-msg');
                // Persist a freshly-typed webhook first so the test uses it.
                if (document.getElementById('discord-webhook')?.value.trim()) {
                    if (!await _discordSave()) return;
                }
                if (msg) { msg.textContent = 'Sending test…'; msg.style.color = 'var(--text-muted)'; }
                try {
                    const r = await fetch('/api/discord/test', { method: 'POST' });
                    if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || 'failed');
                    if (msg) { msg.style.color = 'var(--success)'; msg.textContent = 'Test sent — check your Discord channel.'; }
                } catch (err) {
                    if (msg) { msg.style.color = 'var(--danger)'; msg.textContent = 'Test failed: ' + (err.message || err); }
                }
            });

            // Restore from a full backup .zip.
            const restoreFileInput = document.getElementById('restore-file-input');
            const restoreBtn = document.getElementById('backup-restore-btn');
            if (restoreFileInput && restoreBtn) {
                restoreFileInput.addEventListener('change', () => {
                    restoreBtn.disabled = !restoreFileInput.files.length;
                });
                restoreBtn.addEventListener('click', async () => {
                    if (!confirm('Restore from this backup? It replaces your current database, settings and logins '
                        + '(a safety copy of your current data is kept first). You\'ll need to restart afterwards.')) return;
                    const msg = document.getElementById('backup-msg');
                    restoreBtn.disabled = true;
                    restoreBtn.textContent = 'Restoring…';
                    try {
                        const res = await API.importBackup(restoreFileInput.files[0]);
                        msg.textContent = (res && res.message) || 'Restored. Please restart PawPoller.';
                        msg.style.color = 'var(--success)';
                    } catch (err) {
                        msg.textContent = 'Error: ' + (err.message || err); msg.style.color = 'var(--danger)';
                        restoreBtn.textContent = 'Restore';
                        restoreBtn.disabled = false;
                    }
                });
            }

            // Settings Sync
            const syncResult = document.getElementById('sync-result');
            document.getElementById('sync-pull-btn')?.addEventListener('click', async (e) => {
                e.target.disabled = true; e.target.textContent = 'Pulling...';
                try {
                    // Real REMOTE pull (auto_sync). The old handler hit the local
                    // /sync endpoint and only echoed this device's own settings.
                    const resp = await fetch('/api/settings/sync/pull-now', { method: 'POST' });
                    const data = await resp.json().catch(() => ({}));
                    if (resp.ok && data.ok) {
                        syncResult.innerHTML = data.applied
                            ? '<span style="color:var(--success)">Pulled the server’s settings (' + data.total_keys + ' keys). Reloading…</span>'
                            : '<span style="color:var(--text-muted)">Server reached, but it had nothing newer to pull.</span>';
                        if (data.applied) setTimeout(() => location.reload(), 1200);
                    } else {
                        syncResult.innerHTML = '<span style="color:var(--danger)">' + Utils.escapeHtml(data.detail || 'Pull failed') + '</span>';
                    }
                } catch (err) {
                    syncResult.innerHTML = '<span style="color:var(--danger)">' + Utils.escapeHtml(err.message) + '</span>';
                }
                e.target.disabled = false; e.target.textContent = 'Pull from server';
            });
            document.getElementById('sync-push-btn')?.addEventListener('click', async (e) => {
                e.target.disabled = true; e.target.textContent = 'Pushing...';
                try {
                    const resp = await fetch('/api/settings/sync/push-now', { method: 'POST' });
                    const data = await resp.json().catch(() => ({}));
                    if (resp.ok && data.ok) {
                        syncResult.innerHTML = '<span style="color:var(--success)">Pushed local settings to the server (' + data.keys_merged + ' keys merged).</span>';
                    } else {
                        syncResult.innerHTML = '<span style="color:var(--danger)">' + Utils.escapeHtml(data.detail || 'Push failed') + '</span>';
                    }
                } catch (err) {
                    syncResult.innerHTML = '<span style="color:var(--danger)">' + Utils.escapeHtml(err.message) + '</span>';
                }
                e.target.disabled = false; e.target.textContent = 'Push to server';
            });
            document.getElementById('sync-status-btn')?.addEventListener('click', async (e) => {
                e.target.disabled = true;
                try {
                    const resp = await fetch('/api/settings/sync/status');
                    const data = await resp.json();
                    syncResult.innerHTML = '<code>v' + Utils.escapeHtml(data.version) + '</code> · ' +
                        data.total_keys + ' keys · mode: ' + Utils.escapeHtml(data.credential_mode);
                } catch (err) {
                    syncResult.innerHTML = '<span style="color:var(--danger)">' + Utils.escapeHtml(err.message) + '</span>';
                }
                e.target.disabled = false;
            });

            // Credential Vault — always-on (2.101.0); the card is informational.
            // Just report which key store protects the vault key.
            const vaultResult = document.getElementById('vault-result');
            if (vaultResult) {
                fetch('/api/settings/vault/status')
                    .then(r => r.json())
                    .then(data => {
                        const KEY_LABELS = {
                            operator: 'operator-supplied key (PAWPOLLER_VAULT_KEY — held off this machine\'s data volume)',
                            keyring: 'OS keystore (Windows Credential Manager / Keychain)',
                            dotfile: 'local key file on the data volume — on a server, set PAWPOLLER_VAULT_KEY to hold the key out-of-band',
                        };
                        vaultResult.innerHTML = '<span style="color:var(--success)">✓ Encrypted</span> · key in: '
                            + Utils.escapeHtml(KEY_LABELS[data.key_source] || data.key_source);
                    })
                    .catch(() => { vaultResult.textContent = 'Vault status unavailable'; });
            }

            // Auto-Update
            const applyUpdateBtn = document.getElementById('apply-update-btn');
            if (applyUpdateBtn) {
                applyUpdateBtn.addEventListener('click', async () => {
                    if (!confirm('Download and apply the update? The app will restart.')) return;
                    applyUpdateBtn.disabled = true;
                    applyUpdateBtn.textContent = 'Updating...';
                    try {
                        await API.applyUpdate({ download_url: updateInfo.download_url });
                        applyUpdateBtn.textContent = 'Restarting...';
                    } catch (err) {
                        applyUpdateBtn.textContent = 'Failed';
                        alert('Update failed: ' + err.message);
                    }
                });
            }

            // Check for Updates button (shown when already up to date)
            const checkUpdateBtn = document.getElementById('check-update-btn');
            if (checkUpdateBtn) {
                checkUpdateBtn.addEventListener('click', async () => {
                    checkUpdateBtn.disabled = true;
                    checkUpdateBtn.textContent = 'Checking...';
                    try {
                        const result = await API.checkUpdate();
                        if (result.available) {
                            // Re-render settings to show the update section
                            this.renderSettings();
                        } else {
                            const statusText = document.getElementById('update-status-text');
                            if (statusText) statusText.textContent = 'Up to date';
                            checkUpdateBtn.textContent = 'No updates';
                            setTimeout(() => { checkUpdateBtn.textContent = 'Check for Updates'; checkUpdateBtn.disabled = false; }, 3000);
                        }
                    } catch (err) {
                        checkUpdateBtn.textContent = 'Check failed';
                        setTimeout(() => { checkUpdateBtn.textContent = 'Check for Updates'; checkUpdateBtn.disabled = false; }, 3000);
                    }
                });
            }

            // ── Security tab event handlers ──────────────────────────

            // Change Password
            document.getElementById('sec-change-pw-btn')?.addEventListener('click', async () => {
                const btn = document.getElementById('sec-change-pw-btn');
                const msg = document.getElementById('sec-pw-msg');
                const current = document.getElementById('sec-current-pw').value;
                const newPw = document.getElementById('sec-new-pw').value;
                const confirm = document.getElementById('sec-confirm-pw').value;
                if (!current || !newPw) { msg.textContent = 'All fields are required.'; msg.style.color = 'var(--danger)'; return; }
                btn.disabled = true;
                msg.textContent = '';
                try {
                    await API.dashboardChangePassword({ current_password: current, new_password: newPw, confirm });
                    msg.textContent = 'Password updated.';
                    msg.style.color = 'var(--success)';
                    document.getElementById('sec-current-pw').value = '';
                    document.getElementById('sec-new-pw').value = '';
                    document.getElementById('sec-confirm-pw').value = '';
                } catch (err) {
                    let m = err.message.replace(/^API \d+:\s*/, '');
                    try { m = JSON.parse(m).detail || m; } catch {}
                    msg.textContent = m;
                    msg.style.color = 'var(--danger)';
                }
                btn.disabled = false;
            });

            // TOTP 2FA — load status and render appropriate UI
            this._loadTotpSection();

            // API Keys — load and render
            this._loadApiKeysSection();

            // Sync with server — paired installs only (3.18.0)
            this._loadMirrorSync();

            // Turnstile config — populate from settings
            (async () => {
                try {
                    const ds = await API.getDashboardStatus();
                    const siteKeyEl = document.getElementById('sec-ts-sitekey');
                    if (siteKeyEl && ds.turnstile_site_key) siteKeyEl.value = ds.turnstile_site_key;
                } catch {}
            })();

            document.getElementById('sec-ts-save-btn')?.addEventListener('click', async () => {
                const btn = document.getElementById('sec-ts-save-btn');
                const msg = document.getElementById('sec-ts-msg');
                const siteKey = document.getElementById('sec-ts-sitekey').value.trim();
                const secretKey = document.getElementById('sec-ts-secret').value.trim();
                btn.disabled = true;
                msg.textContent = '';
                try {
                    const result = await API.saveTurnstileConfig({ site_key: siteKey, secret_key: secretKey });
                    msg.textContent = result.message;
                    msg.style.color = 'var(--success)';
                } catch (err) {
                    let m = err.message.replace(/^API \d+:\s*/, '');
                    try { m = JSON.parse(m).detail || m; } catch {}
                    msg.textContent = m;
                    msg.style.color = 'var(--danger)';
                }
                btn.disabled = false;
            });

            // ── Publishing tab event handlers ─────────────────────────
            document.getElementById('save-posting-settings-btn')?.addEventListener('click', async () => {
                const btn = document.getElementById('save-posting-settings-btn');
                const status = document.getElementById('posting-settings-status');
                btn.disabled = true;
                status.textContent = 'Saving...';
                status.style.color = 'var(--text-muted)';
                try {
                    const platformsStr = document.getElementById('posting-default-platforms')?.value || '';
                    const platforms = platformsStr.split(',').map(s => s.trim()).filter(Boolean);
                    await API.savePostingSettings({
                        posting_enabled: document.getElementById('posting-enabled-toggle')?.checked || false,
                        posting_default_rating: document.getElementById('posting-default-rating')?.value || 'adult',
                        posting_default_platforms: platforms,
                        posting_server_url: document.getElementById('posting-server-url')?.value || '',
                        posting_server_api_key: document.getElementById('posting-server-api-key')?.value || '',
                        posting_story_archive_path: document.getElementById('posting-archive-path')?.value || '',
                    });
                    status.textContent = 'Saved!';
                    status.style.color = 'var(--success)';
                } catch (err) {
                    status.textContent = 'Error: ' + err.message;
                    status.style.color = 'var(--danger)';
                }
                btn.disabled = false;
            });

            // "Posted via PawPoller" credit line — self-saving toggle (gap-wave-2 §1).
            // Checking it saves immediately. UNchecking asks nicely first: a small
            // modal (the app's .modal-overlay convention — there's no shared confirm
            // helper) with "Keep it on 💛" (reverts, saves nothing) / "Turn off
            // anyway" (saves off).
            // Watermark settings (gap-wave-5 §1) — loaded from /api/artwork/settings,
            // saved back the same way (artwork_* keys, separate from posting settings).
            API.getArtworkSettings().then(a => {
                const en = document.getElementById('art-wm-enabled'); if (en) en.checked = !!a.artwork_watermark_enabled;
                const tx = document.getElementById('art-wm-text'); if (tx) tx.value = a.artwork_watermark_text || '';
                const po = document.getElementById('art-wm-position'); if (po) po.value = a.artwork_watermark_position || 'bottom-right';
                const op = document.getElementById('art-wm-opacity'); if (op) op.value = a.artwork_watermark_opacity ?? 0.5;
            }).catch(() => {});
            document.getElementById('art-wm-save')?.addEventListener('click', async () => {
                const msg = document.getElementById('art-wm-msg');
                try {
                    await API.saveArtworkSettings({
                        artwork_watermark_enabled: !!document.getElementById('art-wm-enabled')?.checked,
                        artwork_watermark_text: document.getElementById('art-wm-text')?.value.trim() || '',
                        artwork_watermark_position: document.getElementById('art-wm-position')?.value || 'bottom-right',
                        artwork_watermark_opacity: parseFloat(document.getElementById('art-wm-opacity')?.value) || 0.5,
                    });
                    if (msg) { msg.textContent = 'Saved.'; msg.style.color = 'var(--success)'; }
                } catch (err) {
                    if (msg) { msg.textContent = 'Failed: ' + (err.message || err); msg.style.color = 'var(--danger)'; }
                }
            });

            const _attribToggle = document.getElementById('posting-attribution-toggle');
            _attribToggle?.addEventListener('change', async () => {
                if (_attribToggle.checked) {
                    try { await API.savePostingSettings({ pawpoller_attribution: true }); } catch (e) { /* ignore */ }
                    window.toast?.success('Credit line on — thank you! 🐾');
                    return;
                }
                // Unchecking → the plea.
                const ov = document.createElement('div');
                ov.className = 'modal-overlay open';
                ov.innerHTML = `
                    <div class="modal" role="dialog" aria-modal="true" style="max-width:440px">
                        <div class="modal-header"><h3>Before you turn this off…</h3></div>
                        <div class="modal-body">
                            <p>We understand why — it's your art, your page, your description.</p>
                            <p style="margin-top:8px">But that one small line is how other creators find PawPoller.
                            No company, no ads — the credit line is the whole marketing budget.
                            <strong>It would mean a lot if you kept it on.</strong> 🐾</p>
                        </div>
                        <div class="modal-footer" style="display:flex;gap:8px;justify-content:flex-end">
                            <button class="btn btn-outline" id="attrib-off-anyway">Turn off anyway</button>
                            <button class="btn btn-primary" id="attrib-keep-on">Keep it on 💛</button>
                        </div>
                    </div>`;
                document.body.appendChild(ov);
                ov.querySelector('#attrib-keep-on').addEventListener('click', () => {
                    _attribToggle.checked = true;      // revert; nothing saved
                    ov.remove();
                });
                ov.querySelector('#attrib-off-anyway').addEventListener('click', async () => {
                    try { await API.savePostingSettings({ pawpoller_attribution: false }); } catch (e) { /* ignore */ }
                    ov.remove();
                    window.toast?.info('Credit line off. (You can re-enable it here any time.)');
                });
                // Backdrop click = "keep it on" (the safe default).
                ov.addEventListener('click', (e) => {
                    if (e.target === ov) { _attribToggle.checked = true; ov.remove(); }
                });
            });

        } catch (err) {
            this._setContent(`<div class="empty-state"><h3>Error</h3><p>${Utils.escapeHtml(err.message)}</p></div>`);
        }
        // Attach a "Setup guide" button to each platform's connect card
        // (idempotent — safe on every settings re-render).
        if (window.Guides) window.Guides.injectSettingsButtons();
    },

    /* ── Sync with server (3.18.0) ────────────────────────────
     *
     * This replaces a runbook. Before it, syncing a paired desktop meant
     * setting PAWPOLLER_APPDATA_DIR, running scripts/mirror_pull.py from a
     * SOURCE CHECKOUT, and knowing to close the app first — none of it
     * discoverable from the app, and getting the variable wrong silently
     * targeted the checkout's own data instead of the install.
     *
     * Two loads on purpose: /status is instant (no hashing, no network) so the
     * panel renders immediately, and /drift does the real comparison after. */

    async _loadMirrorSync() {
        const body = document.getElementById('mirror-sync-body');
        const badge = document.getElementById('mirror-sync-badge');
        if (!body) return;
        try {
            const st = await API.getMirrorStatus();
            this._mirrorStatus = st;
            body.innerHTML = this._mirrorSyncHtml(st);
            if (badge) {
                badge.textContent = st.pending_database
                    ? '— restart to finish'
                    : (st.last_sync ? '— ' + Utils.formatDateTime(st.last_sync) : '— never synced');
            }
            this._wireMirrorSync();
            this._checkMirrorDrift();
        } catch (err) {
            body.innerHTML = '<p style="color:var(--danger);font-size:13px">Could not read sync status: '
                + Utils.escapeHtml(err.message || String(err)) + '</p>';
        }
    },

    _mirrorSyncHtml(st) {
        const roots = st.roots || {};
        /* The resolved roots are shown because guessing them wrong is the
           mistake that actually happens — artwork does NOT live under
           %APPDATA% by default, and an empty AppData folder beside a populated
           real one reads as "the sync never ran". */
        const pending = st.pending_database ? `
            <div style="margin:12px 0;padding:12px;border:1px solid var(--accent);border-radius:8px">
                <p style="font-size:13px;margin:0 0 8px"><strong>A database update is ready.</strong>
                PawPoller applies it as it starts, because the database cannot be replaced while it is
                open. Your current one is kept as a <code>.bak</code> file.</p>
                <button class="btn btn-sm btn-primary" id="mirror-restart">Restart now</button>
            </div>` : '';

        return `
            <div class="settings-row">
                <div>
                    <span class="settings-label">Server</span>
                    <div style="font-size:11px;color:var(--text-muted);margin-top:2px">
                        <code>${Utils.escapeHtml(st.server_url || '—')}</code>
                        ${st.last_sync ? '&middot; last sync ' + Utils.formatDateTime(st.last_sync) : '&middot; never synced'}
                    </div>
                </div>
                <button class="btn btn-primary" id="mirror-sync-now" ${st.running ? 'disabled' : ''}>Sync now</button>
            </div>

            <div id="mirror-drift-line" style="font-size:13px;color:var(--text-muted);margin:10px 0">Checking what is out of date…</div>
            <div id="mirror-progress" style="display:none;font-size:13px;margin:10px 0"></div>
            ${pending}

            <details style="margin-top:10px">
                <summary style="cursor:pointer;font-size:12px;color:var(--text-muted)">Where this install keeps its files</summary>
                <div style="font-size:11px;color:var(--text-muted);margin-top:8px;line-height:1.7">
                    Artwork: <code>${Utils.escapeHtml(roots.artwork || '')}</code><br>
                    Post media: <code>${Utils.escapeHtml(roots.posts_media || '')}</code><br>
                    Database: <code>${Utils.escapeHtml(roots.database || '')}</code>
                </div>
            </details>

            <div class="settings-row" style="margin-top:12px">
                <div>
                    <span class="settings-label">Check for changes automatically</span>
                    <div style="font-size:11px;color:var(--text-muted);margin-top:2px">
                        Looks for differences in the background and tells you. Never syncs on its own.
                    </div>
                </div>
                <label class="toggle-switch">
                    <input type="checkbox" id="mirror-auto-check" ${st.auto_check ? 'checked' : ''}>
                    <span class="toggle-slider"></span>
                </label>
            </div>`;
    },

    async _checkMirrorDrift() {
        const line = document.getElementById('mirror-drift-line');
        if (!line) return;
        try {
            const d = await API.getMirrorDrift();
            this._mirrorDrift = d;
            if (d.in_sync) {
                line.innerHTML = '<span style="color:var(--success)">✓ Up to date with the server.</span>';
            } else {
                const n = d.files_to_fetch || d.folders_to_fetch;
                const saved = (d.bytes_if_whole_folders || 0) - (d.bytes_to_fetch || 0);
                line.innerHTML = '<strong>' + n + ' item(s)</strong> out of date — '
                    + Utils.formatBytes(d.bytes_to_fetch || 0) + ' to download'
                    + (saved > 0
                        ? ' <span style="color:var(--text-muted)">(only the changed files, not whole folders)</span>'
                        : '');
            }
            if (d.version_match === false) {
                line.innerHTML += '<div style="color:var(--warning);margin-top:6px">Server is '
                    + Utils.escapeHtml(d.remote_version) + ', this app is '
                    + Utils.escapeHtml(d.local_version)
                    + ' — updating one side may avoid surprises.</div>';
            }
            if ((d.local_only || []).length) {
                line.innerHTML += '<div style="color:var(--text-muted);margin-top:6px">'
                    + d.local_only.length
                    + ' item(s) here that the server does not have. A sync never deletes or uploads these.</div>';
            }
        } catch (err) {
            line.innerHTML = '<span style="color:var(--danger)">Could not check: '
                + Utils.escapeHtml(err.message || String(err)) + '</span>';
        }
    },

    _wireMirrorSync() {
        document.getElementById('mirror-sync-now')?.addEventListener('click', async (e) => {
            const btn = e.currentTarget;
            btn.disabled = true;
            btn.textContent = 'Syncing…';
            const prog = document.getElementById('mirror-progress');
            if (prog) { prog.style.display = 'block'; prog.textContent = 'Starting…'; }
            try {
                await API.startMirrorPull({});
                await this._pollMirrorProgress();
            } catch (err) {
                if (prog) {
                    prog.innerHTML = '<span style="color:var(--danger)">'
                        + Utils.escapeHtml(err.message || String(err)) + '</span>';
                }
                btn.disabled = false;
                btn.textContent = 'Sync now';
            }
        });

        document.getElementById('mirror-restart')?.addEventListener('click', async (e) => {
            const msg = 'Restart PawPoller now to finish the sync?\n\nIt will close and reopen by itself.';
            if (!confirm(msg)) return;
            const btn = e.currentTarget;
            btn.disabled = true;
            btn.textContent = 'Restarting…';
            try {
                await API.mirrorRestart();
            } catch (err) {
                /* app.js reports inline, not by toast — there is no _toast on
                   this object. Borrowing masterpieces.js's idiom here would
                   have thrown "this._toast is not a function" at exactly the
                   moment the restart failed. */
                const prog = document.getElementById('mirror-progress');
                if (prog) {
                    prog.style.display = 'block';
                    prog.innerHTML = '<span style="color:var(--danger)">'
                        + Utils.escapeHtml(err.message || String(err)) + '</span>';
                }
                btn.disabled = false;
                btn.textContent = 'Restart now';
            }
        });

        document.getElementById('mirror-auto-check')?.addEventListener('change', async (e) => {
            const box = e.currentTarget;
            try {
                await API.setMirrorAutoCheck(box.checked);
            } catch (err) {
                box.checked = !box.checked;
                const prog = document.getElementById('mirror-progress');
                if (prog) {
                    prog.style.display = 'block';
                    prog.innerHTML = '<span style="color:var(--danger)">'
                        + 'Could not save that setting.</span>';
                }
            }
        });
    },

    async _pollMirrorProgress() {
        const prog = document.getElementById('mirror-progress');
        for (;;) {
            await new Promise(r => setTimeout(r, 700));
            let st;
            try {
                st = await API.getMirrorPullStatus();
            } catch (err) {
                continue;
            }
            if (prog) prog.textContent = st.message || st.phase || 'Working…';
            if (!st.running) {
                const res = st.result || {};
                if (prog) {
                    prog.innerHTML = res.error
                        ? '<span style="color:var(--danger)">' + Utils.escapeHtml(res.error) + '</span>'
                        : '<span style="color:var(--success)">✓ Sync complete.</span>';
                }
                await this._loadMirrorSync();
                return;
            }
        }
    },

    /* ── Security Tab Helpers ─────────────────────────────────
     * Lazy-loaded sections for TOTP and API keys within the Security tab. */

    /* Render one-time 2FA backup codes into #totp-codes-display (gap-wave-4).
     * Shown once — the server only stores hashes. Includes a copy button. */
    _showBackupCodes(codes) {
        const disp = document.getElementById('totp-codes-display');
        if (!disp || !Array.isArray(codes) || !codes.length) return;
        disp.innerHTML = `
            <div style="margin-top:14px;padding:12px;border:1px solid var(--accent);border-radius:8px">
                <p style="font-size:13px;margin:0 0 8px"><strong>🔑 Backup codes</strong> — save these now. Each works once if you lose your authenticator. They won't be shown again.</p>
                <div style="display:grid;grid-template-columns:repeat(2,max-content);gap:4px 24px;font-family:monospace;font-size:14px;margin-bottom:10px">
                    ${codes.map(c => `<span>${Utils.escapeHtml(c)}</span>`).join('')}
                </div>
                <button class="btn btn-sm btn-outline" id="totp-copy-codes">Copy all</button>
            </div>`;
        document.getElementById('totp-copy-codes')?.addEventListener('click', (e) => {
            navigator.clipboard?.writeText(codes.join('\n'));
            e.currentTarget.textContent = 'Copied ✓';
        });
    },

    async _loadTotpSection() {
        const body = document.getElementById('sec-totp-body');
        const badge = document.getElementById('sec-totp-badge');
        if (!body) return;

        try {
            const status = await API.getDashboardStatus();
            const enabled = status.totp_enabled;
            if (badge) badge.textContent = enabled ? '-- Enabled' : '-- Disabled';

            if (enabled) {
                const remaining = status.backup_codes_remaining || 0;
                const lowCodes = remaining <= 2;
                body.innerHTML = `
                    <p style="color:var(--success);font-size:13px;margin-bottom:12px">Two-factor authentication is <strong>enabled</strong>.</p>
                    <div style="font-size:13px;margin-bottom:12px;padding:8px 10px;border-radius:8px;background:${lowCodes ? 'var(--danger-dim,rgba(220,80,80,.12))' : 'var(--bg-hover)'}">
                        🔑 <strong>${remaining}</strong> backup code${remaining === 1 ? '' : 's'} left${lowCodes ? ' — running low, regenerate a fresh set.' : '.'}
                        Use one in place of an authenticator code if you lose your device.
                        <div style="margin-top:8px;display:flex;gap:8px;align-items:center">
                            <input type="password" id="totp-regen-pw" class="search-input" placeholder="Password" style="max-width:220px">
                            <button class="btn btn-sm btn-outline" id="totp-regen-btn">Regenerate backup codes</button>
                        </div>
                    </div>
                    <div id="totp-codes-display"></div>
                    <p style="color:var(--text-muted);font-size:13px;margin-bottom:12px">To disable, enter your password and a current 2FA code (or a backup code).</p>
                    <div class="settings-row" style="flex-direction:column;align-items:stretch;gap:8px">
                        <input type="password" id="totp-disable-pw" class="search-input" placeholder="Password" style="max-width:300px">
                    </div>
                    <div class="settings-row" style="flex-direction:column;align-items:stretch;gap:8px;margin-top:8px">
                        <input type="text" id="totp-disable-code" class="search-input" placeholder="6-digit code or backup code" style="max-width:240px">
                    </div>
                    <div style="margin-top:12px;display:flex;align-items:center;gap:12px">
                        <button class="btn btn-danger" id="totp-disable-btn">Disable 2FA</button>
                        <span id="totp-msg" style="font-size:13px"></span>
                    </div>
                `;
                document.getElementById('totp-regen-btn')?.addEventListener('click', async () => {
                    const pw = document.getElementById('totp-regen-pw').value;
                    const disp = document.getElementById('totp-codes-display');
                    if (!pw) { disp.innerHTML = '<span style="color:var(--danger);font-size:12px">Enter your password first.</span>'; return; }
                    try {
                        const res = await fetch('/api/auth/totp-backup-codes/regenerate', {
                            method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ password: pw }),
                        });
                        const d = await res.json();
                        if (!res.ok) throw new Error(d.detail || 'failed');
                        this._showBackupCodes(d.backup_codes);
                        setTimeout(() => this._loadTotpSection(), 30000);
                    } catch (err) {
                        disp.innerHTML = `<span style="color:var(--danger);font-size:12px">${Utils.escapeHtml(err.message || err)}</span>`;
                    }
                });
                document.getElementById('totp-disable-btn')?.addEventListener('click', async () => {
                    const btn = document.getElementById('totp-disable-btn');
                    const msg = document.getElementById('totp-msg');
                    const password = document.getElementById('totp-disable-pw').value;
                    const code = document.getElementById('totp-disable-code').value.trim();
                    if (!password || !code) { msg.textContent = 'Both fields required.'; msg.style.color = 'var(--danger)'; return; }
                    btn.disabled = true;
                    try {
                        await API.totpDisable({ password, code });
                        msg.textContent = '2FA disabled.';
                        msg.style.color = 'var(--success)';
                        setTimeout(() => this._loadTotpSection(), 1000);
                    } catch (err) {
                        let m = err.message.replace(/^API \d+:\s*/, '');
                        try { m = JSON.parse(m).detail || m; } catch {}
                        msg.textContent = m;
                        msg.style.color = 'var(--danger)';
                        btn.disabled = false;
                    }
                });
            } else {
                body.innerHTML = `
                    <p style="color:var(--text-muted);font-size:13px;margin-bottom:12px">Add an extra layer of security with a TOTP authenticator app (Google Authenticator, Authy, etc).</p>
                    <button class="btn btn-primary" id="totp-setup-btn">Set Up 2FA</button>
                    <div id="totp-setup-area" style="display:none;margin-top:16px">
                        <p style="color:var(--text-muted);font-size:13px">Scan this QR code with your authenticator app, then enter the 6-digit code to verify:</p>
                        <div id="totp-qr" style="margin:16px 0;text-align:center"></div>
                        <div style="margin:8px 0">
                            <label style="font-size:12px;color:var(--text-muted)">Manual entry key:</label>
                            <code id="totp-secret" style="display:block;margin-top:4px;font-size:13px;word-break:break-all;color:var(--text-primary)"></code>
                        </div>
                        <div class="settings-row" style="flex-direction:column;align-items:stretch;gap:8px;margin-top:12px">
                            <input type="text" id="totp-verify-code" class="search-input" placeholder="6-digit code" style="max-width:200px" inputmode="numeric" maxlength="6">
                            <input type="password" id="totp-verify-pw" class="search-input" placeholder="Your account password" style="max-width:280px">
                        </div>
                        <div style="margin-top:12px;display:flex;align-items:center;gap:12px">
                            <button class="btn btn-success" id="totp-verify-btn">Verify & Enable</button>
                            <span id="totp-msg" style="font-size:13px"></span>
                        </div>
                        <div id="totp-codes-display"></div>
                    </div>
                `;
                document.getElementById('totp-setup-btn')?.addEventListener('click', async () => {
                    const btn = document.getElementById('totp-setup-btn');
                    btn.disabled = true;
                    btn.textContent = 'Generating...';
                    try {
                        const result = await API.totpSetup();
                        document.getElementById('totp-setup-area').style.display = 'block';
                        document.getElementById('totp-secret').textContent = result.secret;
                        btn.style.display = 'none';

                        // Generate QR code using simple text representation
                        const qrDiv = document.getElementById('totp-qr');
                        if (window.QRCode) {
                            new window.QRCode(qrDiv, { text: result.uri, width: 200, height: 200, colorDark: '#ffffff', colorLight: '#1a1a2e' });
                        } else {
                            // Fallback: show the URI as copyable text
                            qrDiv.innerHTML = `<p style="font-size:12px;color:var(--text-muted)">QR library not loaded. Copy the manual key above into your authenticator app.</p>`;
                        }
                    } catch (err) {
                        btn.textContent = 'Set Up 2FA';
                        btn.disabled = false;
                    }
                });

                // Delegate verify button (created dynamically)
                body.addEventListener('click', async (e) => {
                    if (!e.target.matches('#totp-verify-btn')) return;
                    const btn = e.target;
                    const msg = document.getElementById('totp-msg');
                    const code = document.getElementById('totp-verify-code').value.trim();
                    const password = document.getElementById('totp-verify-pw')?.value || '';
                    if (!code) { msg.textContent = 'Enter the 6-digit code.'; msg.style.color = 'var(--danger)'; return; }
                    if (!password) { msg.textContent = 'Enter your account password.'; msg.style.color = 'var(--danger)'; return; }
                    btn.disabled = true;
                    try {
                        const res = await API.totpEnable({ code, password });
                        msg.textContent = '2FA enabled! Save your backup codes below.';
                        msg.style.color = 'var(--success)';
                        // Show the one-time backup codes; leave them up (don't auto-reload
                        // the section) so the user can copy them before they're gone.
                        this._showBackupCodes(res && res.backup_codes);
                    } catch (err) {
                        let m = err.message.replace(/^API \d+:\s*/, '');
                        try { m = JSON.parse(m).detail || m; } catch {}
                        msg.textContent = m;
                        msg.style.color = 'var(--danger)';
                        btn.disabled = false;
                    }
                });
            }
        } catch (err) {
            body.innerHTML = `<p style="color:var(--danger);font-size:13px">Failed to load 2FA status.</p>`;
        }
    },

    async _loadApiKeysSection() {
        const body = document.getElementById('sec-apikeys-body');
        if (!body) return;

        try {
            const data = await API.getApiKeys();
            const keys = data.keys || [];

            let html = `
                <p style="color:var(--text-muted);font-size:13px;margin-bottom:12px">API keys allow programmatic access (e.g. from scripts or Claude). Keys are shown once on creation.</p>
                <div style="display:flex;gap:8px;align-items:center;margin-bottom:16px;flex-wrap:wrap">
                    <input type="text" id="apikey-name" class="search-input" placeholder="Key name (e.g. 'Claude', 'Curl')" style="max-width:250px">
                    <button class="btn btn-primary" id="apikey-create-btn">Generate Key</button>
                </div>
                <div id="apikey-created-area" style="display:none;margin-bottom:16px;padding:12px;background:var(--bg-primary);border-radius:8px;border:1px solid var(--success)">
                    <p style="font-size:13px;color:var(--success);margin-bottom:8px"><strong>Key created!</strong> Copy it now — it won't be shown again.</p>
                    <code id="apikey-created-value" style="display:block;font-size:13px;word-break:break-all;color:var(--text-primary);user-select:all"></code>
                    <button class="btn btn-secondary" id="apikey-copy-btn" style="margin-top:8px;padding:4px 12px;font-size:12px">Copy</button>
                </div>
            `;

            if (keys.length > 0) {
                html += `<table class="submissions-table" style="width:100%;font-size:13px">
                    <thead><tr><th>Name</th><th>Prefix</th><th>Created</th><th></th></tr></thead>
                    <tbody>`;
                for (const k of keys) {
                    html += `<tr>
                        <td>${Utils.escapeHtml(k.name)}</td>
                        <td><code>${Utils.escapeHtml(k.prefix)}...</code></td>
                        <td>${k.created ? new Date(k.created).toLocaleDateString() : '—'}</td>
                        <td><button class="btn btn-danger" data-revoke-prefix="${Utils.escapeHtml(k.prefix)}" style="padding:2px 10px;font-size:12px">Revoke</button></td>
                    </tr>`;
                }
                html += `</tbody></table>`;
            } else {
                html += `<p style="color:var(--text-muted);font-size:13px">No API keys configured.</p>`;
            }

            body.innerHTML = html;

            // Create key
            document.getElementById('apikey-create-btn')?.addEventListener('click', async () => {
                const btn = document.getElementById('apikey-create-btn');
                const name = document.getElementById('apikey-name').value.trim();
                if (!name) { alert('Key name is required.'); return; }
                btn.disabled = true;
                try {
                    const result = await API.createApiKey({ name });
                    const area = document.getElementById('apikey-created-area');
                    area.style.display = 'block';
                    document.getElementById('apikey-created-value').textContent = result.key;
                    document.getElementById('apikey-name').value = '';
                    // Don't auto-refresh — let the user copy the key first.
                    // The table refreshes on next page load or manual refresh.
                } catch (err) {
                    alert('Failed to create key: ' + err.message);
                }
                btn.disabled = false;
            });

            // Copy button
            body.addEventListener('click', (e) => {
                if (e.target.matches('#apikey-copy-btn')) {
                    const val = document.getElementById('apikey-created-value')?.textContent;
                    if (val) {
                        navigator.clipboard.writeText(val).then(() => {
                            e.target.textContent = 'Copied!';
                            setTimeout(() => { e.target.textContent = 'Copy'; }, 2000);
                        });
                    }
                }
            });

            // Revoke buttons
            body.querySelectorAll('[data-revoke-prefix]').forEach(btn => {
                btn.addEventListener('click', async () => {
                    const prefix = btn.dataset.revokePrefix;
                    if (!confirm(`Revoke API key ${prefix}...? This cannot be undone.`)) return;
                    btn.disabled = true;
                    try {
                        await API.revokeApiKey(prefix);
                        this._loadApiKeysSection();
                    } catch (err) {
                        alert('Failed to revoke: ' + err.message);
                        btn.disabled = false;
                    }
                });
            });
        } catch (err) {
            body.innerHTML = `<p style="color:var(--danger);font-size:13px">Failed to load API keys.</p>`;
        }
    },

    // ── Auto-refresh ──────────────────────────────────────────
    // 60-second polling loop that re-renders the current page to show fresh
    // data from the latest poll cycle. Skips the re-render when the browser
    // tab is hidden (document.hidden) to avoid wasted API calls. Preserves
    // the user's scroll position across refreshes. Shows a brief "Refreshing..."
    // toast indicator during the update. Cleared on every route change via
    // _stopAutoRefresh() so stale timers don't fire on the wrong page.

    _startAutoRefresh(renderFn) {
        this._stopAutoRefresh();
        this._autoRefreshTimer = setInterval(async () => {
            if (document.hidden) return;
            const indicator = document.getElementById('refresh-indicator');
            if (indicator) indicator.style.opacity = '1';
            const scrollY = window.scrollY;
            try {
                await renderFn();
                window.scrollTo(0, scrollY);
            } catch { /* ignore — next tick will retry */ }
            const ind2 = document.getElementById('refresh-indicator');
            if (ind2) setTimeout(() => { ind2.style.opacity = '0'; }, 800);
        }, this._autoRefreshInterval);
    },

    // Clears both the auto-refresh timer and the loading-screen poll interval.
    // Called at the start of every route change (in route()) and at the start
    // of _startAutoRefresh() to prevent timer stacking.
    _stopAutoRefresh() {
        if (this._autoRefreshTimer) {
            clearInterval(this._autoRefreshTimer);
            this._autoRefreshTimer = null;
        }
        if (this._loadingPollInterval) {
            clearInterval(this._loadingPollInterval);
            this._loadingPollInterval = null;
        }
    },

    // Returns the HTML for the "Refreshing..." toast indicator. Placed at the
    // top of every page's HTML template. Starts invisible (opacity:0 in CSS)
    // and is briefly faded in by _startAutoRefresh during a refresh cycle.
    _refreshIndicatorHtml() {
        return '<div id="refresh-indicator" class="refresh-indicator">Refreshing...</div>';
    },

    // ── Helpers ────────────────────────────────────────────────
    // Shared utility methods used across multiple page renderers. These handle
    // common UI patterns: date range bar binding, table column sorting,
    // client-side search/filter, and per-platform variants of each.

    // Binds click handlers to the date range bar buttons (All / 7d / 30d / 90d / Year).
    // Updates _dateRange state, toggles the .active CSS class on the clicked button,
    // and invokes the callback (which typically re-fetches data with the new range).
    _bindDateRange(callback) {
        const bar = document.getElementById('date-range-bar');
        if (!bar) return;
        bar.querySelectorAll('.range-btn').forEach(btn => {
            btn.addEventListener('click', () => {
                this._dateRange = btn.dataset.range;
                bar.querySelectorAll('.range-btn').forEach(b => b.classList.remove('active'));
                btn.classList.add('active');
                callback();
            });
        });
    },

    // View toggle: grid ↔ list. Stores preference in localStorage, swaps visibility.
    _bindViewToggle() {
        document.querySelectorAll('.view-toggle-btn').forEach(btn => {
            btn.addEventListener('click', () => {
                const mode = btn.dataset.view;
                localStorage.setItem('pp-view-mode', mode);
                document.querySelectorAll('.view-toggle-btn').forEach(b => b.classList.remove('active'));
                btn.classList.add('active');
                const grid = document.getElementById('grid-container');
                const table = document.getElementById('table-container');
                if (grid) grid.style.display = mode === 'grid' ? '' : 'none';
                if (table) table.style.display = mode === 'list' ? '' : 'none';
            });
        });
    },

    // Binds click handlers to sortable column headers in the IB submissions table.
    // Clicking a header toggles asc/desc if already sorted by that column, or
    // switches to that column in desc order. Triggers a full re-render via
    // renderSubmissions() which re-fetches with the new sort from the API.
    _bindTableSort() {
        document.querySelectorAll('#submissions-table th[data-sort]').forEach(th => {
            th.addEventListener('click', () => {
                const field = th.dataset.sort;
                if (this._sortState.field === field) {
                    this._sortState.order = this._sortState.order === 'desc' ? 'asc' : 'desc';
                } else {
                    this._sortState.field = field;
                    this._sortState.order = 'desc';
                }
                this.renderSubmissions();
            });
        });
    },

    // Client-side search and filter for the IB submissions table. Filters by:
    // - Text query: matches against title and keywords (case-insensitive substring)
    // - Rating: matches numeric rating_id (0=General, 1=Mature, 2=Adult)
    // - Type: matches type_name (Picture/Pinup, Writing, etc.)
    // Operates on the full allSubmissions array (already fetched) rather than
    // re-fetching from the API, so filtering is instant. Re-renders the table
    // HTML and re-binds sort handlers after each filter change.
    _bindSearch(allSubmissions, gridRenderer) {
        const input = document.getElementById('search-input');
        const ratingSelect = document.getElementById('filter-rating');
        const typeSelect = document.getElementById('filter-type');

        const doFilter = () => {
            const q = (input?.value || '').toLowerCase();
            const rating = ratingSelect?.value || '';
            const type = typeSelect?.value || '';

            let filtered = allSubmissions;
            if (q) {
                filtered = filtered.filter(s =>
                    (s.title || '').toLowerCase().includes(q) || (s.keywords || '').toLowerCase().includes(q)
                );
            }
            if (rating) {
                filtered = filtered.filter(s => String(s.rating_id) === rating);
            }
            if (type) {
                filtered = filtered.filter(s => (s.type_name || '') === type);
            }

            // 2.16.14 (BUG-021): always re-render BOTH containers so the
            // search works whichever view-mode the user is in. The grid
            // renderer is platform-specific so it's passed in as a
            // closure by the caller; null in legacy callers leaves the
            // grid alone (only the table view filters).
            const grid = document.getElementById('grid-container');
            if (grid && gridRenderer) grid.innerHTML = gridRenderer(filtered);
            document.getElementById('table-container').innerHTML = Components.submissionsTable(filtered);
            this._bindTableSort();
        };

        input?.addEventListener('input', doFilter);
        ratingSelect?.addEventListener('change', doFilter);
        typeSelect?.addEventListener('change', doFilter);
    },

    // FA variant of _bindTableSort(). Same toggle-sort pattern but targets
    // #fa-submissions-table headers and uses _faSortState + renderFASubmissions().
    _bindFATableSort() {
        document.querySelectorAll('#fa-submissions-table th[data-sort]').forEach(th => {
            th.addEventListener('click', () => {
                const field = th.dataset.sort;
                if (this._faSortState.field === field) {
                    this._faSortState.order = this._faSortState.order === 'desc' ? 'asc' : 'desc';
                } else {
                    this._faSortState.field = field;
                    this._faSortState.order = 'desc';
                }
                this.renderFASubmissions();
            });
        });
    },

    // FA variant of _bindSearch(). Filters by text (title/keywords) and rating
    // (General/Mature/Adult string values instead of IB's numeric rating_id).
    // No type filter (FA doesn't have IB's type_name taxonomy).
    _bindFASearch(allSubmissions, gridRenderer) {
        const input = document.getElementById('search-input');
        const ratingSelect = document.getElementById('filter-rating');

        const doFilter = () => {
            const q = (input?.value || '').toLowerCase();
            const rating = ratingSelect?.value || '';

            let filtered = allSubmissions;
            if (q) {
                filtered = filtered.filter(s =>
                    (s.title || '').toLowerCase().includes(q) || (s.keywords || '').toLowerCase().includes(q)
                );
            }
            if (rating) {
                filtered = filtered.filter(s => (s.rating || '') === rating);
            }

            // 2.16.14 (BUG-021): re-render grid container too if a renderer was passed
            const grid = document.getElementById('grid-container');
            if (grid && gridRenderer) grid.innerHTML = gridRenderer(filtered);
            document.getElementById('table-container').innerHTML = Components.faSubmissionsTable(filtered);
            this._bindFATableSort();
        };

        input?.addEventListener('input', doFilter);
        ratingSelect?.addEventListener('change', doFilter);
    },

    // WS variant of _bindTableSort(). Targets #ws-submissions-table headers,
    // uses _wsSortState + renderWSSubmissions().
    _bindWSTableSort() {
        document.querySelectorAll('#ws-submissions-table th[data-sort]').forEach(th => {
            th.addEventListener('click', () => {
                const field = th.dataset.sort;
                if (this._wsSortState.field === field) {
                    this._wsSortState.order = this._wsSortState.order === 'desc' ? 'asc' : 'desc';
                } else {
                    this._wsSortState.field = field;
                    this._wsSortState.order = 'desc';
                }
                this.renderWSSubmissions();
            });
        });
    },

    // WS variant of _bindSearch(). Filters by text (title/keywords) and rating
    // (General/Mature/Explicit — Weasyl's terminology). No type filter.
    _bindWSSearch(allSubmissions, gridRenderer) {
        const input = document.getElementById('search-input');
        const ratingSelect = document.getElementById('filter-rating');

        const doFilter = () => {
            const q = (input?.value || '').toLowerCase();
            const rating = ratingSelect?.value || '';

            let filtered = allSubmissions;
            if (q) {
                filtered = filtered.filter(s =>
                    (s.title || '').toLowerCase().includes(q) || (s.keywords || '').toLowerCase().includes(q)
                );
            }
            if (rating) {
                filtered = filtered.filter(s => (s.rating || '') === rating);
            }

            // 2.16.14 (BUG-021): re-render grid container too if a renderer was passed
            const grid = document.getElementById('grid-container');
            if (grid && gridRenderer) grid.innerHTML = gridRenderer(filtered);
            document.getElementById('table-container').innerHTML = Components.wsSubmissionsTable(filtered);
            this._bindWSTableSort();
        };

        input?.addEventListener('input', doFilter);
        ratingSelect?.addEventListener('change', doFilter);
    },

    // SF variant of _bindTableSort(). Targets #sf-submissions-table headers,
    // uses _sfSortState + renderSFSubmissions().
    _bindSFTableSort() {
        document.querySelectorAll('#sf-submissions-table th[data-sort]').forEach(th => {
            th.addEventListener('click', () => {
                const field = th.dataset.sort;
                if (this._sfSortState.field === field) {
                    this._sfSortState.order = this._sfSortState.order === 'desc' ? 'asc' : 'desc';
                } else {
                    this._sfSortState.field = field;
                    this._sfSortState.order = 'desc';
                }
                this.renderSFSubmissions();
            });
        });
    },

    // SQW variant of _bindTableSort(). Targets #sqw-submissions-table headers,
    // uses _sqwSortState + renderSQWSubmissions().
    _bindSQWTableSort() {
        document.querySelectorAll('#sqw-submissions-table th[data-sort]').forEach(th => {
            th.addEventListener('click', () => {
                const field = th.dataset.sort;
                if (this._sqwSortState.field === field) {
                    this._sqwSortState.order = this._sqwSortState.order === 'desc' ? 'asc' : 'desc';
                } else {
                    this._sqwSortState.field = field;
                    this._sqwSortState.order = 'desc';
                }
                this.renderSQWSubmissions();
            });
        });
    },

    // SQW variant of _bindSearch(). Filters by text (title/tags) and rating.
    _bindSQWSearch(allSubmissions, gridRenderer) {
        const input = document.getElementById('search-input');
        const ratingSelect = document.getElementById('filter-rating');

        const doFilter = () => {
            const q = (input?.value || '').toLowerCase();
            const rating = ratingSelect?.value || '';

            let filtered = allSubmissions;
            if (q) {
                filtered = filtered.filter(s =>
                    (s.title || '').toLowerCase().includes(q) || (s.keywords || '').toLowerCase().includes(q)
                );
            }
            if (rating) {
                filtered = filtered.filter(s => (s.rating || '') === rating);
            }

            // 2.16.14 (BUG-021): re-render grid container too if a renderer was passed
            const grid = document.getElementById('grid-container');
            if (grid && gridRenderer) grid.innerHTML = gridRenderer(filtered);
            document.getElementById('table-container').innerHTML = Components.sqwSubmissionsTable(filtered);
            this._bindSQWTableSort();
        };

        input?.addEventListener('input', doFilter);
        ratingSelect?.addEventListener('change', doFilter);
    },

    // AO3 variant of _bindTableSort(). Targets #ao3-submissions-table headers,
    // uses _ao3SortState + renderAO3Submissions().
    _bindAO3TableSort() {
        document.querySelectorAll('#ao3-submissions-table th[data-sort]').forEach(th => {
            th.addEventListener('click', () => {
                const field = th.dataset.sort;
                if (this._ao3SortState.field === field) {
                    this._ao3SortState.order = this._ao3SortState.order === 'desc' ? 'asc' : 'desc';
                } else {
                    this._ao3SortState.field = field;
                    this._ao3SortState.order = 'desc';
                }
                this.renderAO3Submissions();
            });
        });
    },

    // AO3 variant of _bindSearch(). Filters by text (title/tags) and rating.
    _bindAO3Search(allSubmissions, gridRenderer) {
        const input = document.getElementById('search-input');
        const ratingSelect = document.getElementById('filter-rating');

        const doFilter = () => {
            const q = (input?.value || '').toLowerCase();
            const rating = ratingSelect?.value || '';

            let filtered = allSubmissions;
            if (q) {
                filtered = filtered.filter(s =>
                    (s.title || '').toLowerCase().includes(q) || (s.keywords || '').toLowerCase().includes(q)
                );
            }
            if (rating) {
                filtered = filtered.filter(s => (s.rating || '') === rating);
            }

            // 2.16.14 (BUG-021): re-render grid container too if a renderer was passed
            const grid = document.getElementById('grid-container');
            if (grid && gridRenderer) grid.innerHTML = gridRenderer(filtered);
            document.getElementById('table-container').innerHTML = Components.ao3SubmissionsTable(filtered);
            this._bindAO3TableSort();
        };

        input?.addEventListener('input', doFilter);
        ratingSelect?.addEventListener('change', doFilter);
    },

    // DA variant of _bindTableSort(). Targets #da-submissions-table headers,
    // uses _daSortState + renderDASubmissions().
    _bindDATableSort() {
        document.querySelectorAll('#da-submissions-table th[data-sort]').forEach(th => {
            th.addEventListener('click', () => {
                const field = th.dataset.sort;
                if (this._daSortState.field === field) {
                    this._daSortState.order = this._daSortState.order === 'desc' ? 'asc' : 'desc';
                } else {
                    this._daSortState.field = field;
                    this._daSortState.order = 'desc';
                }
                this.renderDASubmissions();
            });
        });
    },

    // DA variant of _bindSearch(). Filters by text (title/keywords) and rating.
    _bindDASearch(allSubmissions, gridRenderer) {
        const input = document.getElementById('search-input');
        const ratingSelect = document.getElementById('filter-rating');

        const doFilter = () => {
            const q = (input?.value || '').toLowerCase();
            const rating = ratingSelect?.value || '';

            let filtered = allSubmissions;
            if (q) {
                filtered = filtered.filter(s =>
                    (s.title || '').toLowerCase().includes(q) || (s.keywords || '').toLowerCase().includes(q)
                );
            }
            if (rating) {
                filtered = filtered.filter(s => (s.rating || '') === rating);
            }

            // 2.16.14 (BUG-021): re-render grid container too if a renderer was passed
            const grid = document.getElementById('grid-container');
            if (grid && gridRenderer) grid.innerHTML = gridRenderer(filtered);
            document.getElementById('table-container').innerHTML = Components.daSubmissionsTable(filtered);
            this._bindDATableSort();
        };

        input?.addEventListener('input', doFilter);
        ratingSelect?.addEventListener('change', doFilter);
    },

    // WP variant of _bindTableSort(). Targets #wp-submissions-table headers,
    // uses _wpSortState + renderWPSubmissions().
    _bindWPTableSort() {
        document.querySelectorAll('#wp-submissions-table th[data-sort]').forEach(th => {
            th.addEventListener('click', () => {
                const field = th.dataset.sort;
                if (this._wpSortState.field === field) {
                    this._wpSortState.order = this._wpSortState.order === 'desc' ? 'asc' : 'desc';
                } else {
                    this._wpSortState.field = field;
                    this._wpSortState.order = 'desc';
                }
                this.renderWPSubmissions();
            });
        });
    },

    // WP variant of _bindSearch(). Filters by text (title only — Wattpad has no rating filter).
    _bindWPSearch(allSubmissions, gridRenderer) {
        const input = document.getElementById('search-input');

        const doFilter = () => {
            const q = (input?.value || '').toLowerCase();

            let filtered = allSubmissions;
            if (q) {
                filtered = filtered.filter(s =>
                    (s.title || '').toLowerCase().includes(q)
                );
            }

            // 2.16.14 (BUG-021): re-render grid container too if a renderer was passed
            const grid = document.getElementById('grid-container');
            if (grid && gridRenderer) grid.innerHTML = gridRenderer(filtered);
            document.getElementById('table-container').innerHTML = Components.wpSubmissionsTable(filtered);
            this._bindWPTableSort();
        };

        input?.addEventListener('input', doFilter);
    },

    // IK variant of _bindTableSort(). Targets #ik-submissions-table headers,
    // uses _ikSortState + renderIKSubmissions().
    _bindIKTableSort() {
        document.querySelectorAll('#ik-submissions-table th[data-sort]').forEach(th => {
            th.addEventListener('click', () => {
                const field = th.dataset.sort;
                if (this._ikSortState.field === field) {
                    this._ikSortState.order = this._ikSortState.order === 'desc' ? 'asc' : 'desc';
                } else {
                    this._ikSortState.field = field;
                    this._ikSortState.order = 'desc';
                }
                this.renderIKSubmissions();
            });
        });
    },

    // IK variant of _bindSearch(). Filters by text (title only).
    _bindIKSearch(allSubmissions, gridRenderer) {
        const input = document.getElementById('search-input');

        const doFilter = () => {
            const q = (input?.value || '').toLowerCase();

            let filtered = allSubmissions;
            if (q) {
                filtered = filtered.filter(s =>
                    (s.title || '').toLowerCase().includes(q)
                );
            }

            // 2.16.14 (BUG-021): re-render grid container too if a renderer was passed
            const grid = document.getElementById('grid-container');
            if (grid && gridRenderer) grid.innerHTML = gridRenderer(filtered);
            document.getElementById('table-container').innerHTML = Components.ikSubmissionsTable(filtered);
            this._bindIKTableSort();
        };

        input?.addEventListener('input', doFilter);
    },

    // BSKY table sort binding — same pattern as IK but for BSKY.
    _bindBSKYTableSort() {
        document.querySelectorAll('#bsky-submissions-table th[data-sort]').forEach(th => {
            th.addEventListener('click', () => {
                const field = th.dataset.sort;
                if (this._bskySortState.field === field) {
                    this._bskySortState.order = this._bskySortState.order === 'desc' ? 'asc' : 'desc';
                } else {
                    this._bskySortState.field = field;
                    this._bskySortState.order = 'desc';
                }
                this.renderBSKYSubmissions();
            });
        });
    },

    // BSKY variant of _bindSearch(). Filters by text (title only).
    _bindBSKYSearch(allSubmissions, gridRenderer) {
        const input = document.getElementById('search-input');

        const doFilter = () => {
            const q = (input?.value || '').toLowerCase();

            let filtered = allSubmissions;
            if (q) {
                filtered = filtered.filter(s =>
                    (s.title || '').toLowerCase().includes(q)
                );
            }

            // 2.16.14 (BUG-021): re-render grid container too if a renderer was passed
            const grid = document.getElementById('grid-container');
            if (grid && gridRenderer) grid.innerHTML = gridRenderer(filtered);
            document.getElementById('table-container').innerHTML = Components.bskySubmissionsTable(filtered);
            this._bindBSKYTableSort();
        };

        input?.addEventListener('input', doFilter);
    },

    // MAST table sort binding — same pattern as BSKY but for MAST.
    _bindMASTTableSort() {
        document.querySelectorAll('#mast-submissions-table th[data-sort]').forEach(th => {
            th.addEventListener('click', () => {
                const field = th.dataset.sort;
                if (this._mastSortState.field === field) {
                    this._mastSortState.order = this._mastSortState.order === 'desc' ? 'asc' : 'desc';
                } else {
                    this._mastSortState.field = field;
                    this._mastSortState.order = 'desc';
                }
                this.renderMASTSubmissions();
            });
        });
    },

    // MAST variant of _bindSearch(). Filters by text (title only).
    _bindMASTSearch(allSubmissions, gridRenderer) {
        const input = document.getElementById('search-input');

        const doFilter = () => {
            const q = (input?.value || '').toLowerCase();

            let filtered = allSubmissions;
            if (q) {
                filtered = filtered.filter(s =>
                    (s.title || '').toLowerCase().includes(q)
                );
            }

            const grid = document.getElementById('grid-container');
            if (grid && gridRenderer) grid.innerHTML = gridRenderer(filtered);
            document.getElementById('table-container').innerHTML = Components.mastSubmissionsTable(filtered);
            this._bindMASTTableSort();
        };

        input?.addEventListener('input', doFilter);
    },

    // TUM table sort binding — same pattern as MAST but for TUM.
    _bindTUMTableSort() {
        document.querySelectorAll('#tum-submissions-table th[data-sort]').forEach(th => {
            th.addEventListener('click', () => {
                const field = th.dataset.sort;
                if (this._tumSortState.field === field) {
                    this._tumSortState.order = this._tumSortState.order === 'desc' ? 'asc' : 'desc';
                } else {
                    this._tumSortState.field = field;
                    this._tumSortState.order = 'desc';
                }
                this.renderTUMSubmissions();
            });
        });
    },

    // TUM variant of _bindSearch(). Filters by text (title only).
    _bindTUMSearch(allSubmissions, gridRenderer) {
        const input = document.getElementById('search-input');

        const doFilter = () => {
            const q = (input?.value || '').toLowerCase();

            let filtered = allSubmissions;
            if (q) {
                filtered = filtered.filter(s =>
                    (s.title || '').toLowerCase().includes(q)
                );
            }

            const grid = document.getElementById('grid-container');
            if (grid && gridRenderer) grid.innerHTML = gridRenderer(filtered);
            document.getElementById('table-container').innerHTML = Components.tumSubmissionsTable(filtered);
            this._bindTUMTableSort();
        };

        input?.addEventListener('input', doFilter);
    },

    // PIX table sort binding — same pattern as the gallery platforms.
    _bindPIXTableSort() {
        document.querySelectorAll('#pix-submissions-table th[data-sort]').forEach(th => {
            th.addEventListener('click', () => {
                const field = th.dataset.sort;
                if (this._pixSortState.field === field) {
                    this._pixSortState.order = this._pixSortState.order === 'desc' ? 'asc' : 'desc';
                } else {
                    this._pixSortState.field = field;
                    this._pixSortState.order = 'desc';
                }
                this.renderPIXSubmissions();
            });
        });
    },

    // PIX variant of _bindSearch(). Filters by text (title only).
    _bindPIXSearch(allSubmissions, gridRenderer) {
        const input = document.getElementById('search-input');

        const doFilter = () => {
            const q = (input?.value || '').toLowerCase();

            let filtered = allSubmissions;
            if (q) {
                filtered = filtered.filter(s =>
                    (s.title || '').toLowerCase().includes(q)
                );
            }

            const grid = document.getElementById('grid-container');
            if (grid && gridRenderer) grid.innerHTML = gridRenderer(filtered);
            document.getElementById('table-container').innerHTML = Components.pixSubmissionsTable(filtered);
            this._bindPIXTableSort();
        };

        input?.addEventListener('input', doFilter);
    },

    // THR table sort binding.
    _bindTHRTableSort() {
        document.querySelectorAll('#thr-submissions-table th[data-sort]').forEach(th => {
            th.addEventListener('click', () => {
                const field = th.dataset.sort;
                if (this._thrSortState.field === field) {
                    this._thrSortState.order = this._thrSortState.order === 'desc' ? 'asc' : 'desc';
                } else {
                    this._thrSortState.field = field;
                    this._thrSortState.order = 'desc';
                }
                this.renderTHRSubmissions();
            });
        });
    },

    // THR variant of _bindSearch(). Filters by text (title only).
    _bindTHRSearch(allSubmissions, gridRenderer) {
        const input = document.getElementById('search-input');

        const doFilter = () => {
            const q = (input?.value || '').toLowerCase();

            let filtered = allSubmissions;
            if (q) {
                filtered = filtered.filter(s =>
                    (s.title || '').toLowerCase().includes(q)
                );
            }

            const grid = document.getElementById('grid-container');
            if (grid && gridRenderer) grid.innerHTML = gridRenderer(filtered);
            document.getElementById('table-container').innerHTML = Components.thrSubmissionsTable(filtered);
            this._bindTHRTableSort();
        };

        input?.addEventListener('input', doFilter);
    },

    // IG table sort binding.
    _bindIGTableSort() {
        document.querySelectorAll('#ig-submissions-table th[data-sort]').forEach(th => {
            th.addEventListener('click', () => {
                const field = th.dataset.sort;
                if (this._igSortState.field === field) {
                    this._igSortState.order = this._igSortState.order === 'desc' ? 'asc' : 'desc';
                } else {
                    this._igSortState.field = field;
                    this._igSortState.order = 'desc';
                }
                this.renderIGSubmissions();
            });
        });
    },

    // IG variant of _bindSearch(). Filters by text (title only).
    _bindIGSearch(allSubmissions, gridRenderer) {
        const input = document.getElementById('search-input');

        const doFilter = () => {
            const q = (input?.value || '').toLowerCase();

            let filtered = allSubmissions;
            if (q) {
                filtered = filtered.filter(s =>
                    (s.title || '').toLowerCase().includes(q)
                );
            }

            const grid = document.getElementById('grid-container');
            if (grid && gridRenderer) grid.innerHTML = gridRenderer(filtered);
            document.getElementById('table-container').innerHTML = Components.igSubmissionsTable(filtered);
            this._bindIGTableSort();
        };

        input?.addEventListener('input', doFilter);
    },

    // TW table sort binding — same pattern as IK but for TW.
    _bindTWTableSort() {
        document.querySelectorAll('#tw-submissions-table th[data-sort]').forEach(th => {
            th.addEventListener('click', () => {
                const field = th.dataset.sort;
                if (this._twSortState.field === field) {
                    this._twSortState.order = this._twSortState.order === 'desc' ? 'asc' : 'desc';
                } else {
                    this._twSortState.field = field;
                    this._twSortState.order = 'desc';
                }
                this.renderTWSubmissions();
            });
        });
    },

    // TW variant of _bindSearch(). Filters by text (title only).
    _bindTWSearch(allSubmissions, gridRenderer) {
        const input = document.getElementById('search-input');

        const doFilter = () => {
            const q = (input?.value || '').toLowerCase();

            let filtered = allSubmissions;
            if (q) {
                filtered = filtered.filter(s =>
                    (s.title || '').toLowerCase().includes(q)
                );
            }

            // 2.16.14 (BUG-021): re-render grid container too if a renderer was passed
            const grid = document.getElementById('grid-container');
            if (grid && gridRenderer) grid.innerHTML = gridRenderer(filtered);
            document.getElementById('table-container').innerHTML = Components.twSubmissionsTable(filtered);
            this._bindTWTableSort();
        };

        input?.addEventListener('input', doFilter);
    },

    // SF variant of _bindSearch(). Filters by text (title/keywords) and rating
    // (Clean/Mature/Adult — SoFurry's terminology).
    _bindSFSearch(allSubmissions, gridRenderer) {
        const input = document.getElementById('search-input');
        const ratingSelect = document.getElementById('filter-rating');

        const doFilter = () => {
            const q = (input?.value || '').toLowerCase();
            const rating = ratingSelect?.value || '';

            let filtered = allSubmissions;
            if (q) {
                filtered = filtered.filter(s =>
                    (s.title || '').toLowerCase().includes(q) || (s.keywords || '').toLowerCase().includes(q)
                );
            }
            if (rating) {
                filtered = filtered.filter(s => (s.rating || '') === rating);
            }

            // 2.16.14 (BUG-021): re-render grid container too if a renderer was passed
            const grid = document.getElementById('grid-container');
            if (grid && gridRenderer) grid.innerHTML = gridRenderer(filtered);
            document.getElementById('table-container').innerHTML = Components.sfSubmissionsTable(filtered);
            this._bindSFTableSort();
        };

        input?.addEventListener('input', doFilter);
        ratingSelect?.addEventListener('change', doFilter);
    },

    // ── Pin/Goal/Tag action helpers ──────────────────────────

    // Binds click handlers for unpin buttons and goal delete buttons on dashboards
    _bindPinAndGoalActions(rerender) {
        document.querySelectorAll('.pinned-card[data-nav]').forEach(card => {
            card.addEventListener('click', (e) => {
                if (e.target.closest('.btn-unpin')) return;
                const nav = card.dataset.nav;
                if (nav) this.navigate('/' + nav);
            });
            card.style.cursor = 'pointer';
        });
        document.querySelectorAll('.btn-unpin').forEach(btn => {
            btn.addEventListener('click', async (e) => {
                e.stopPropagation();
                try {
                    await API.removePin(btn.dataset.platform, btn.dataset.id);
                    rerender();
                } catch (err) { alert('Failed to unpin: ' + err.message); }
            });
        });
        document.querySelectorAll('.btn-goal-delete').forEach(btn => {
            btn.addEventListener('click', async () => {
                if (!confirm('Delete this goal?')) return;
                try {
                    await API.deleteGoal(parseInt(btn.dataset.goalId));
                    rerender();
                } catch (err) { alert('Failed to delete goal: ' + err.message); }
            });
        });
    },

    // Binds pin toggle button and tag add button on detail pages
    _bindDetailPinTag(platform, subId, allTags, rerender) {
        document.querySelectorAll('.btn-pin').forEach(btn => {
            btn.addEventListener('click', async () => {
                const isPinned = btn.textContent.trim() === 'Unpin';
                try {
                    if (isPinned) {
                        await API.removePin(platform, subId);
                    } else {
                        await API.addPin({ platform, submission_id: subId });
                    }
                    rerender();
                } catch (err) { alert('Failed: ' + err.message); }
            });
        });
        document.querySelectorAll('.btn-add-tag').forEach(btn => {
            btn.addEventListener('click', async () => {
                if (!allTags.length) {
                    const name = prompt('Create a new tag (enter name):');
                    if (!name) return;
                    try {
                        const result = await API.createTag({ name });
                        await API.addTagToSubmission(result.tag_id, { platform, submission_id: subId });
                        rerender();
                    } catch (err) { alert('Failed: ' + err.message); }
                    return;
                }
                const options = allTags.map(t => `${t.tag_id}: ${t.name}`).join('\n');
                const choice = prompt(`Enter tag ID to add (or "new" to create):\n${options}`);
                if (!choice) return;
                try {
                    if (choice.toLowerCase() === 'new') {
                        const name = prompt('New tag name:');
                        if (!name) return;
                        const result = await API.createTag({ name });
                        await API.addTagToSubmission(result.tag_id, { platform, submission_id: subId });
                    } else {
                        const tagId = parseInt(choice);
                        if (isNaN(tagId)) return;
                        await API.addTagToSubmission(tagId, { platform, submission_id: subId });
                    }
                    rerender();
                } catch (err) { alert('Failed: ' + err.message); }
            });
        });
        document.querySelectorAll('.tag-badge[data-tag-id]').forEach(badge => {
            badge.style.cursor = 'pointer';
            badge.title = 'Click to remove tag';
            badge.addEventListener('click', async () => {
                try {
                    await API.removeTagFromSubmission(parseInt(badge.dataset.tagId), platform, subId);
                    rerender();
                } catch (err) { alert('Failed: ' + err.message); }
            });
        });
    },

    // ── Analytics Page ──────────────────────────────────────────

    /* Tag performance (gap-wave-6). Deterministic — no AI: each piece is scored
     * against its own platform's median, so `index` 1.0 = a typical piece for
     * that platform; >1 = the tag tends to beat it. Green ≥1.15, muted <0.85. */
    _tagPerfHtml(tp) {
        if (!tp || !(tp.tags || []).length) return '';
        const esc = Utils.escapeHtml;
        const idxBadge = (v) => {
            const col = v >= 1.15 ? 'var(--success)' : (v < 0.85 ? 'var(--text-muted)' : 'var(--text)');
            return `<span style="font-weight:600;color:${col}">${(v).toFixed(2)}×</span>`;
        };
        const tagRows = tp.tags.map(t => `<tr>
            <td>${esc(t.tag)}</td>
            <td style="text-align:right">${idxBadge(t.index)}</td>
            <td style="text-align:right">${Utils.formatNumber(t.works)}</td>
            <td style="text-align:right">${Utils.formatNumber(t.avg_reach)}</td>
            <td style="text-align:right">${Utils.formatNumber(t.faves)}</td>
        </tr>`).join('');
        const pairRows = (tp.pairs || []).map(p => `<tr>
            <td>${esc(p.tags.join(' + '))}</td>
            <td style="text-align:right">${idxBadge(p.index)}</td>
            <td style="text-align:right">${Utils.formatNumber(p.works)}</td>
        </tr>`).join('');
        return `
            <div class="chart-container">
                <h3>Tag performance <span class="muted" style="font-size:.7em">how much each tag beats a typical post on its platform · index 1.0 = average · ≥${tp.min_works} works</span></h3>
                <div class="table-scroll"><table class="data-table">
                    <thead><tr><th>Tag</th><th style="text-align:right">Index</th><th style="text-align:right">Works</th><th style="text-align:right">Avg reach</th><th style="text-align:right">Faves</th></tr></thead>
                    <tbody>${tagRows}</tbody></table></div>
                ${pairRows ? `<h4 style="margin:.9rem 0 .4rem">Best combinations</h4>
                <div class="table-scroll"><table class="data-table">
                    <thead><tr><th>Tag pair</th><th style="text-align:right">Index</th><th style="text-align:right">Works</th></tr></thead>
                    <tbody>${pairRows}</tbody></table></div>` : ''}
            </div>`;
    },

    /* Benchmarks + best-time sections (gap-wave-3 §2+3). Relative-engagement
     * buckets: 1.0 = a typical post on that platform; buckets with < 3 posts
     * are greyed (thin evidence, not advice). */
    _insightsHtml(ins) {
        if (!ins || !Object.keys(ins.platforms || {}).length) return '';
        const esc = Utils.escapeHtml;
        const over = (ins.overperformers || []).map(o => `<tr>
            <td>${esc(o.platform)}</td><td>${esc(o.title)}</td>
            <td>${Utils.formatNumber(o.value)} ${esc(o.metric)}</td>
            <td><strong>${o.ratio}×</strong> median</td></tr>`).join('');
        const plats = Object.entries(ins.platforms).map(([c, p]) => `<tr>
            <td>${esc(c)}</td><td>${Utils.formatNumber(p.median)} ${esc(p.metric)}</td>
            <td class="muted">${p.count} pieces</td></tr>`).join('');
        const bar = (b, label) => {
            const thin = b.count < 3;
            const w = Math.min(100, Math.round((b.median || 0) * 50));
            return `<div style="display:flex;align-items:center;gap:6px;font-size:11px;${thin ? 'opacity:.35' : ''}"
                title="${b.count} post(s)"><span style="width:32px;text-align:right">${label}</span>
                <div style="flex:1;background:var(--bg-hover);border-radius:3px;height:10px">
                    <div style="width:${w}%;height:10px;border-radius:3px;background:var(--accent)"></div>
                </div><span class="muted" style="width:58px">${b.median || 0}× · n=${b.count}</span></div>`;
        };
        const days = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'];
        const weekdayBars = (ins.weekday || []).map((b, i) => bar(b, days[i])).join('');
        const hourBars = (ins.hour || []).map((b, i) => bar(b, String(i).padStart(2, '0'))).join('');
        return `
            <div class="chart-container">
                <h3>Benchmarks</h3>
                ${over ? `<h4 style="margin:.4rem 0">Overperformers (vs their platform's median)</h4>
                <div class="table-scroll"><table class="data-table">
                    <thead><tr><th>Platform</th><th>Title</th><th>Engagement</th><th>vs median</th></tr></thead>
                    <tbody>${over}</tbody></table></div>` : ''}
                <h4 style="margin:.8rem 0 .4rem">Platform medians</h4>
                <div class="table-scroll"><table class="data-table">
                    <thead><tr><th>Platform</th><th>Median per piece</th><th>Sample</th></tr></thead>
                    <tbody>${plats}</tbody></table></div>
            </div>
            <div class="chart-container">
                <h3>When your audience responds <span class="muted" style="font-size:.7em">relative engagement by local posting time; greyed = under 3 posts</span></h3>
                <div style="display:grid;grid-template-columns:1fr 1fr;gap:18px">
                    <div><h4 style="margin:.2rem 0">By weekday</h4>${weekdayBars}</div>
                    <div><h4 style="margin:.2rem 0">By hour posted</h4>${hourBars}</div>
                </div>
            </div>`;
    },

    /* Repost Radar (gap-wave-6) — older, well-performing artwork worth resurfacing.
     * Deterministic: the server ranks YOUR past art by pooled engagement, gated to
     * pieces you haven't posted in _repostRadarAge days. No model, no AI. */
    _repostRadarAge: 60,

    _repostAgeLabel(days) {
        if (days == null) return '';
        if (days >= 730) return `${Math.floor(days / 365)} years ago`;
        if (days >= 365) return 'about a year ago';
        if (days >= 60) return `${Math.round(days / 30)} months ago`;
        if (days >= 14) return `${Math.round(days / 7)} weeks ago`;
        return `${days} days ago`;
    },

    async renderRepostRadar() {
        this._loading();
        const age = this._repostRadarAge;
        let res;
        try {
            res = await API.getRepostRadar({ min_age_days: age, limit: 40 });
        } catch (e) {
            this._setContent('<div class="empty-state"><h3>Repost Radar</h3>'
                + '<p>Could not load candidates. Try again shortly.</p></div>');
            return;
        }
        const cands = (res && res.candidates) || [];
        const foll = (res && res.followers) || [];

        const ageOpts = [30, 60, 90, 180, 365].map(d =>
            `<option value="${d}"${d === age ? ' selected' : ''}>Not shared in ${d}+ days</option>`
        ).join('');

        const follStrip = foll.length ? `
            <div class="chart-container" style="margin-bottom:16px">
                <h3 style="margin:0 0 .2rem">Your following now
                    <span class="muted" style="font-size:.7em">growth since tracking began — fills in over time</span></h3>
                <div style="display:flex;flex-wrap:wrap;gap:8px;margin-top:.5rem">
                    ${foll.map(f => {
                        const label = this._platformLabels[f.platform] || (f.platform || '').toUpperCase();
                        const now = f.followers != null ? Utils.formatNumber(f.followers) : '--';
                        let delta = '';
                        if (f.delta != null && f.days != null) {
                            const sign = f.delta > 0 ? '+' : '';
                            delta = ` <span style="color:${f.delta > 0 ? 'var(--success,#4caf50)' : 'var(--text-muted)'}">`
                                + `${sign}${Utils.formatNumber(f.delta)} · ${f.days}d</span>`;
                        }
                        return `<span class="chip" style="padding:.35em .7em">
                            <strong>${Utils.escapeHtml(label)}</strong> ${now}${delta}</span>`;
                    }).join('')}
                </div>
            </div>` : '';

        const cardsHtml = cands.length ? cands.map(c => {
            const thumb = c.thumb_url
                ? `<img src="${c.thumb_url}" alt="" loading="lazy"
                     style="width:100%;height:170px;object-fit:cover;display:block">`
                : `<div style="height:170px;display:flex;align-items:center;justify-content:center;
                     background:var(--bg-glass);color:var(--text-muted);font-size:2rem">&#127912;</div>`;
            const chips = (c.platforms || []).map(p => {
                const label = this._platformLabels[p.platform] || (p.platform || '').toUpperCase();
                return p.url
                    ? `<a class="chip" href="${Utils.escapeHtml(p.url)}" target="_blank" rel="noopener"
                         style="padding:.25em .6em;text-decoration:none">${Utils.escapeHtml(label)} &#8599;</a>`
                    : `<span class="chip" style="padding:.25em .6em">${Utils.escapeHtml(label)}</span>`;
            }).join('');
            return `
                <div class="submission-card" style="cursor:default">
                    <a href="${c.detail_route}" style="text-decoration:none;color:inherit;display:block">${thumb}</a>
                    <div style="padding:12px">
                        <a href="${c.detail_route}" style="text-decoration:none;color:inherit">
                            <div style="font-weight:600;line-height:1.3;margin-bottom:2px">${Utils.escapeHtml(c.title || c.name)}</div>
                        </a>
                        <div class="muted" style="font-size:12px;margin-bottom:8px">posted ~${this._repostAgeLabel(c.age_days)}</div>
                        <div style="display:flex;gap:12px;font-size:13px;margin-bottom:10px">
                            <span title="views">&#128065; ${Utils.formatNumber(c.views)}</span>
                            <span title="faves">&#10084;&#65039; ${Utils.formatNumber(c.faves)}</span>
                            <span title="comments">&#128172; ${Utils.formatNumber(c.comments)}</span>
                        </div>
                        <div style="display:flex;flex-wrap:wrap;gap:6px">${chips}</div>
                    </div>
                </div>`;
        }).join('') : `<div class="empty-state" style="grid-column:1/-1">
                <h3>Nothing to resurface yet</h3>
                <p>No artwork is older than ${age} days with engagement to show. Lower the age filter,
                   or check back once your older pieces have had time to gather views.</p>
            </div>`;

        this._setContent(`
            <div class="page-header">
                <h2>&#128260; Repost Radar</h2>
                <div class="field" style="margin-left:auto;margin-bottom:0">
                    <select id="repost-age-select" style="width:auto">${ageOpts}</select>
                </div>
            </div>
            <p class="muted" style="margin:-.4rem 0 1rem;max-width:70ch">
                Your best old artwork, ranked by the views, faves and comments it actually earned —
                surfaced so you can share it again to your feed. Purely your own numbers and post dates;
                no AI involved.
            </p>
            ${follStrip}
            <div class="submission-card-grid">${cardsHtml}</div>
        `);

        const sel = document.getElementById('repost-age-select');
        if (sel) sel.addEventListener('change', () => {
            this._repostRadarAge = parseInt(sel.value, 10) || 60;
            this.renderRepostRadar();
        });
    },

    async renderAnalytics() {
        this._loading();
        try {
            const [data, insights, tagPerf] = await Promise.all([
                API.getHistoricalAnalytics({ weeks: 12 }),
                fetch('/api/analytics/insights?tz_offset=' + (-new Date().getTimezoneOffset()))
                    .then(r => r.ok ? r.json() : null).catch(() => null),
                API.getTagPerformance({ min_works: 3, limit: 30 }).catch(() => null),
            ]);

            const bestMonth = data.best_month || {};
            const fastest = data.fastest_growing || [];
            const weekly = data.weekly_growth || [];

            const html = `
                ${this._refreshIndicatorHtml()}
                <div class="page-header">
                    <h2>Analytics</h2>
                    <div style="margin-left:auto;display:flex;gap:0.5em">
                        <button class="btn btn-sm btn-primary" id="analytics-export-all" title="Download the complete dataset: every work × platform with all its stats">&darr; Full data CSV</button>
                        <button class="btn btn-sm btn-outline" id="analytics-export-fastest" ${fastest.length ? '' : 'disabled'} title="Download fastest-growing table as CSV">&darr; Fastest CSV</button>
                        <button class="btn btn-sm btn-outline" id="analytics-export-weekly" ${weekly.length ? '' : 'disabled'} title="Download weekly growth as CSV">&darr; Weekly CSV</button>
                        <button class="btn btn-sm btn-outline" id="analytics-export-chart" ${weekly.length ? '' : 'disabled'} title="Download the chart as PNG">&darr; Chart PNG</button>
                    </div>
                </div>

                <div class="stats-grid">
                    ${Components.highlightCard('Best Month (Views)', bestMonth.views ? `+${Utils.formatNumber(bestMonth.views.delta)}` : '--', bestMonth.views ? bestMonth.views.period : '')}
                    ${Components.highlightCard('Best Month (Faves)', bestMonth.faves ? `+${Utils.formatNumber(bestMonth.faves.delta)}` : '--', bestMonth.faves ? bestMonth.faves.period : '')}
                    ${Components.highlightCard('Best Month (Comments)', bestMonth.comments ? `+${Utils.formatNumber(bestMonth.comments.delta)}` : '--', bestMonth.comments ? bestMonth.comments.period : '')}
                </div>

                ${fastest.length ? `
                <div class="chart-container">
                    <h3>Fastest Growing All-Time</h3>
                    <div class="table-scroll">
                        <table class="data-table">
                            <thead><tr><th>Platform</th><th>Title</th><th>Views</th><th>Faves</th><th>Growth/Day</th></tr></thead>
                            <tbody>
                                ${fastest.map(f => {
                                    const isIK = (f.platform || '').toLowerCase() === 'ik';
                                    const isWP = (f.platform || '').toLowerCase() === 'wp';
                                    return `<tr>
                                    <td>${Utils.escapeHtml(f.platform || '')}</td>
                                    <td>${Utils.escapeHtml(f.title || '')}</td>
                                    <td>${isIK ? '--' : Utils.formatNumber(isWP ? (f.reads || f.views || 0) : (f.views || 0))}</td>
                                    <td>${Utils.formatNumber(f.faves || 0)}</td>
                                    <td>${(f.views_per_day || 0).toFixed(1)} ${isIK ? 'likes' : isWP ? 'reads' : 'views'}</td>
                                </tr>`;
                                }).join('')}
                            </tbody>
                        </table>
                    </div>
                </div>` : ''}

                ${weekly.length ? `
                <div class="chart-container">
                    <h3>Weekly Growth (Last 12 Weeks)</h3>
                    <div class="chart-wrap" style="min-height:300px"><canvas id="chart-weekly-growth"></canvas></div>
                </div>` : ''}
                ${this._insightsHtml(insights)}
                ${this._tagPerfHtml(tagPerf)}
            `;

            this._setContent(html);

            let weeklyChart = null;
            if (weekly.length) {
                weeklyChart = Charts.weeklyGrowthBar('chart-weekly-growth', weekly);
            }

            // Complete dataset → server-built CSV (every work × platform + all
            // stats). Same-origin navigation so the session cookie rides (gap G5).
            document.getElementById('analytics-export-all')?.addEventListener('click', () => {
                window.location.href = '/api/works/export.csv';
            });
            // Export wiring — the summary tables export client-side via Utils.downloadCSV.
            // Fastest-growing table → CSV via Utils.downloadCSV.
            document.getElementById('analytics-export-fastest')?.addEventListener('click', () => {
                Utils.downloadCSV(
                    ['Platform', 'Title', 'Views', 'Faves', 'Growth/Day', 'Unit'],
                    fastest.map(f => {
                        const isIK = (f.platform || '').toLowerCase() === 'ik';
                        const isWP = (f.platform || '').toLowerCase() === 'wp';
                        return [
                            f.platform || '',
                            f.title || '',
                            isIK ? '' : (isWP ? (f.reads || f.views || 0) : (f.views || 0)),
                            f.faves || 0,
                            (f.views_per_day || 0).toFixed(2),
                            isIK ? 'likes' : isWP ? 'reads' : 'views',
                        ];
                    }),
                    `pawpoller-fastest-growing-${Utils.dateStamp()}.csv`,
                );
            });
            // Weekly growth → CSV.
            document.getElementById('analytics-export-weekly')?.addEventListener('click', () => {
                Utils.downloadCSV(
                    ['Week start', 'Views', 'Faves', 'Comments'],
                    weekly.map(w => [
                        w.week || w.period || '',
                        w.views || 0,
                        w.faves || 0,
                        w.comments || 0,
                    ]),
                    `pawpoller-weekly-growth-${Utils.dateStamp()}.csv`,
                );
            });
            // Chart → PNG via Chart.js's toBase64Image.
            document.getElementById('analytics-export-chart')?.addEventListener('click', () => {
                if (!weeklyChart || typeof weeklyChart.toBase64Image !== 'function') return;
                const a = document.createElement('a');
                a.href = weeklyChart.toBase64Image('image/png', 1);
                a.download = `pawpoller-weekly-growth-${Utils.dateStamp()}.png`;
                document.body.appendChild(a);
                a.click();
                a.remove();
            });
        } catch (err) {
            this._setContent(`<div class="empty-state"><h3>Error loading analytics</h3><p>${Utils.escapeHtml(err.message)}</p></div>`);
        }
    },

    // Sidebar footer status check. Called on a 60-second interval set up
    // in init(). Fetches the latest poll log entry and updates the small
    // "Last poll: X ago" badge at the bottom of the sidebar. Failures are
    // silently ignored since this is purely cosmetic.
    async _updateStatusCheck() {
        try {
            const [status, pauseState] = await Promise.all([
                API.getStatus(),
                API.getPollPaused().catch(() => ({ polling_paused: false })),
            ]);
            const el = document.getElementById('poll-status-mini');
            if (el) {
                if (pauseState.polling_paused) {
                    el.textContent = 'Polling paused';
                    el.style.color = 'var(--warning, #f0a050)';
                } else if (status.last_poll) {
                    el.textContent = `Last poll: ${Utils.timeAgo(status.last_poll.started_at)}`;
                    el.style.color = '';
                }
            }
        } catch { /* ignore */ }
    },

    // ── Global Sync Progress Bar ─────────────────────────────
    // Checks all platform progress endpoints on a timer. When any platform
    // is actively syncing, shows a thin progress bar at the top of the page.
    // Uses a fast interval (1.5s) when active, slow (10s) when idle.

    _progressCheckActive: false,
    _progressCheckTimer: null,

    _initProgressCheckBar() {
        this._progressCheckTick();
        if (this._progressCheckTimer) clearInterval(this._progressCheckTimer);
        this._progressCheckTimer = setInterval(() => this._progressCheckTick(), 10000);
    },

    async _progressCheckTick() {
        const bar = document.getElementById('poll-progress-bar');
        const fill = document.getElementById('poll-progress-fill');
        const label = document.getElementById('poll-progress-label');
        if (!bar || !fill || !label) return;

        try {
            // 2.16.9: one fetch instead of 9. The combined endpoint
            // returns { ib, fa, ws, sf, sqw, ao3, da, wp, ik, bsky, tw }
            // — same shape per platform as the old per-platform calls.
            // Single .catch keeps the bar quiet on transient auth blips
            // instead of spamming 9 console errors.
            const all = await API.getAllPollProgress().catch(() => ({}));

            const platforms = [
                { name: 'Inkbunny', data: all.ib || null },
                { name: 'FurAffinity', data: all.fa || null },
                { name: 'Weasyl', data: all.ws || null },
                { name: 'SoFurry', data: all.sf || null },
                { name: 'SquidgeWorld', data: all.sqw || null },
                { name: 'AO3', data: all.ao3 || null },
                { name: 'DeviantArt', data: all.da || null },
                { name: 'Wattpad', data: all.wp || null },
                { name: 'Itaku', data: all.ik || null },
            ];

            const active = platforms.filter(p => p.data && p.data.active);

            if (active.length === 0) {
                bar.style.display = 'none';
                if (this._progressCheckActive) {
                    this._progressCheckActive = false;
                    clearInterval(this._progressCheckTimer);
                    this._progressCheckTimer = setInterval(() => this._progressCheckTick(), 10000);
                }
                return;
            }

            // Switch to fast checking when active
            if (!this._progressCheckActive) {
                this._progressCheckActive = true;
                clearInterval(this._progressCheckTimer);
                this._progressCheckTimer = setInterval(() => this._progressCheckTick(), 1500);
            }

            bar.style.display = 'block';

            // Aggregate progress: average across active pollers
            let totalPct = 0;
            const labels = [];
            for (const p of active) {
                const d = p.data;
                let pct = 0;
                if (d.total > 0) {
                    pct = Math.round((d.current / d.total) * 100);
                } else if (d.phase === 'complete') {
                    pct = 100;
                } else if (d.phase === 'starting' || d.phase === 'logging_in') {
                    pct = 10;
                } else if (d.phase === 'searching') {
                    pct = 20;
                } else if (d.phase === 'fetching_details') {
                    pct = 35;
                } else {
                    pct = 5;
                }
                totalPct += pct;
                const msg = d.message || d.phase || '';
                if (d.total > 0) {
                    labels.push(`${p.name}: ${d.current}/${d.total}`);
                } else {
                    labels.push(`${p.name}: ${msg}`);
                }
            }

            const avgPct = Math.round(totalPct / active.length);
            fill.style.width = Math.min(100, Math.max(2, avgPct)) + '%';
            label.textContent = labels.join('  ·  ');

        } catch { /* ignore */ }
    },
};

// Boot
document.addEventListener('DOMContentLoaded', () => App.init());
