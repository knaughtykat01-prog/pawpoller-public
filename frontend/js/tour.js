/* PawPoller guided tours — a lightweight coach-mark / spotlight overlay with
 * zero dependencies. One engine drives many tours: a "getting-started" tour of
 * the app shell, plus one tour per page.
 *
 * Design choices worth knowing across sessions:
 *   - The dim + spotlight is a single box-shadow trick: `.pp-tour-spot` is a
 *     small box over the target whose enormous spread shadow paints everything
 *     outside it dark, and a `.pp-tour-blocker` swallows background clicks so
 *     the tour drives navigation via its own Next/Back. Centered steps (no
 *     target) hide the spot and dim the blocker instead.
 *   - Page tours target each page's DURABLE chrome — headers, toolbars, filter
 *     bars, action buttons, list/grid CONTAINERS, empty-state cards — never a
 *     data row/card, because a new user's pages are empty. Some steps target
 *     state-exclusive elements (`.empty-state` exists only when empty; a
 *     `.data-table`/grid exists only when populated), so the engine SKIPS any
 *     step whose target is missing or hidden, in whichever direction you're
 *     moving. That makes each tour correct for both empty and populated pages.
 *   - "Seen" is persisted BOTH server-side (settings.json `tours_seen`, via
 *     GET/POST /api/settings/tour-seen) AND in per-browser localStorage
 *     (`pp_tour_done` for getting-started, `pp_tour_done__<page>` for pages).
 *     The server is the source of truth so a dismissal follows the user across
 *     Safari, the installed PWA, the desktop app and updates; localStorage is a
 *     synchronous cache + offline fallback (older per-origin behaviour, which
 *     re-showed tours on any fresh store — an iOS PWA gets storage separate
 *     from Safari, so that was the reappearing-guides cause). `hydrate()` pulls
 *     the server set once at login, mirrors it into localStorage, and pushes up
 *     any locally-dismissed tours it doesn't yet know about (one-time migration).
 *     Auto-fire is gated: it AWAITS hydrate() so a server-seen tour never fires
 *     before the set has loaded; getting-started fires once on the overview; a
 *     page tour fires once on first visit, but only AFTER getting-started is
 *     done, and never immediately on the heels of another tour (a short
 *     debounce). Replaying via the sidebar "?" ignores the flag.
 *
 * Public API (window.Tour):
 *   start(name, opts)   run a named tour now (ignores the seen flag)
 *   startHere(opts)     run the tour for the current route (the "?" button)
 *   maybeAuto(hash)     auto-fire hook, called from App.route()
 *   end(completed)      tear down + persist the seen flag (local + server)
 *   isDone(name)        has this tour been seen/dismissed (server set ∪ local)
 *   hydrate()           load the server-side seen set (memoised); call at login
 *   tourForHash(hash)   map a location hash to a tour name (or null)
 */
window.Tour = (function () {
    'use strict';

    const GS = 'getting-started';
    function doneKey(name) { return name === GS ? 'pp_tour_done' : 'pp_tour_done__' + name; }

    /* ── Server-backed "seen" set ────────────────────────────────────────
     * `_serverSeen` is the set of tour names the server knows are dismissed,
     * null until hydrated. localStorage stays as a synchronous cache/offline
     * fallback; the server is the source of truth so a dismissal follows the
     * user across browsers, the installed PWA and updates. */
    let _serverSeen = null;   // Set<string> | null
    let _hydrateP = null;     // memoised hydrate promise (reset on auth/network fail)

    function localDone(name) {
        try { return localStorage.getItem(doneKey(name)) === '1'; } catch (e) { return false; }
    }
    function markLocal(name) {
        try { localStorage.setItem(doneKey(name), '1'); } catch (e) { /* private mode */ }
    }
    /* Fire-and-forget: tell the server this tour is seen. Additive server-side,
     * so losing this request just means it retries via reconcile() next login. */
    function postSeen(name) {
        try {
            fetch('/api/settings/tour-seen', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                credentials: 'same-origin',
                body: JSON.stringify({ name: name }),
            }).catch(function () {});
        } catch (e) { /* ignore */ }
    }
    /* One-time migration: any tour dismissed on THIS browser before server
     * persistence existed gets pushed up so it sticks everywhere. */
    function reconcile() {
        if (!_serverSeen) return;
        Object.keys(TOURS).forEach(function (name) {
            if (localDone(name) && !_serverSeen.has(name)) {
                _serverSeen.add(name);
                postSeen(name);
            }
        });
    }
    /* Load the server seen-set once. Memoised, but on an auth (401/403) or
     * network failure we clear the memo so a later call (post-login) retries —
     * otherwise a pre-login attempt would cache an empty set forever. */
    function hydrate() {
        if (_hydrateP) return _hydrateP;
        _hydrateP = (async function () {
            try {
                const r = await fetch('/api/settings/preferences', { credentials: 'same-origin' });
                if (r.status === 401 || r.status === 403) {
                    _hydrateP = null;                 // not logged in yet — allow a retry
                    if (!_serverSeen) _serverSeen = new Set();
                    return;
                }
                if (r.ok) {
                    const p = await r.json();
                    const list = Array.isArray(p.tours_seen) ? p.tours_seen : [];
                    _serverSeen = new Set(list);
                    list.forEach(markLocal);          // mirror server → local cache
                    reconcile();                      // push any local-only dismissals up
                } else if (!_serverSeen) {
                    _serverSeen = new Set();
                }
            } catch (e) {
                _hydrateP = null;                     // network blip — retry later
                if (!_serverSeen) _serverSeen = new Set();
            }
        })();
        return _hydrateP;
    }

    /* ── Tour registry ──────────────────────────────────────────────────
     * getting-started walks the persistent shell chrome; each page tour walks
     * one page. Step shape: { target: <cssSelector|null>, title, body }.
     * body may contain <em>; keep it one short sentence. */
    const TOURS = {
        'getting-started': [
            { target: null, title: 'Welcome to PawPoller 👋', body: 'PawPoller tracks and publishes your stories and art across every site you use, from one place. Here’s a quick tour of the essentials — about a minute.' },
            { target: '.nav-link[data-page="platforms"]', title: 'Platforms', body: 'Start here. Connect the sites you use — Inkbunny, FurAffinity, AO3, Bluesky and more. PawPoller only ever tracks the platforms you connect.' },
            // One Library step, because there's now one works hub (2.155.0). These
            // were two steps targeting data-page="submissions" (hub retired 2.117.0)
            // and data-page="posting" (retired 2.155.0) — both nav entries are gone,
            // so both steps were silently targeting nothing.
            { target: '.nav-link[data-page="library"]', title: 'Library', body: 'Every work you track — stories and artwork alike — lives here, with views, faves and comments pulled in from each platform. Filter by type, or review what polling has discovered.' },
            { target: '.nav-link[data-page="inbox"]', title: 'Inbox', body: 'Comments from every site in one list. Reply from here where the site allows it, or tick them off as handled.' },
            { target: '.nav-link[data-page="posts"]', title: 'Posts', body: 'Short posts and journals: write once, send to Bluesky, X, Mastodon, Threads, Tumblr and more, now or at a time you pick.' },
            { target: '.nav-link[data-page="editor"]', title: 'Story Editor', body: 'Write or import a story, tag it per platform, then run a Publish Check to catch problems <em>before</em> anything goes live.' },
            { target: '.nav-link[data-page="analytics"]', title: 'Analytics', body: 'Views, favourites and comments over time — combined across every platform, or broken down site by site.' },
            { target: '#poll-status-mini', title: 'Polling', body: 'PawPoller checks your platforms on a schedule and refreshes these numbers on its own. This badge shows the current cycle at a glance.' },
            { target: '#pp-notif-bell', title: 'The bell', body: 'What PawPoller did and found: posts that went out or failed, new comments, sign-ins that need redoing. Coming back after a while? An <em>Overnight</em> summary opens on its own.' },
            { target: '.nav-link[data-page="settings"]', title: 'Settings', body: 'Your time zone, how often each site is checked, notifications, security and backups — it’s all in here.' },
            { target: '#account-menu-btn', title: 'Tours live here', body: 'That’s the shell. Every page has its own tour too: open this menu and pick “Take the tour” any time to run through wherever you are.' },
            { target: null, title: 'You’re all set 🎉', body: 'The best first step is to connect a platform, then add your first story.<br><br><a href="#/platforms" class="pp-tour-link" data-tour-go>Connect a platform →</a>', cta: 'Finish' },
        ],

        'platforms': [
            { target: null, title: 'Welcome to Platforms', body: 'Every site you’ve connected, with its numbers, in one place. Open one for its own dashboard.' },
            { target: '#ph-att-h', title: 'Needs attention', body: 'Only shows when something’s wrong — a sign-in that ran out, or a site that stopped answering — with the button to fix it.' },
            { target: '#ph-mine-h', title: 'Your platforms', body: 'One tile per connected site with its audience and works. Click a tile to open that site’s dashboard; ⋯ checks it now or opens its setup guide.' },
            { target: '.ph-tools', title: 'Tiles or a list', body: 'Switch between tiles and a list, and sort by biggest audience, A–Z or your own order.' },
            { target: '[data-act="choose"]', title: 'Choose platforms', body: 'Pick which sites PawPoller shows. Sites you don’t use stay out of every page and menu.' },
            { target: '#ph-unset-h', title: 'Not set up yet', body: 'Sites you could add. <em>Set up</em> walks you through it step by step; ✕ hides one you’ll never use.' },
            { target: '.logo-disclaimer', title: 'A quick disclaimer', body: 'PawPoller is independent. Platform names and logos belong to their owners and just help you spot each service.' },
        ],

        /* One tour for the one works hub (2.155.0). It replaces three that had
         * outlived their pages: 'submissions' (hub retired 2.117.0), 'stories'
         * (#/posting, retired 2.155.0) and 'artwork' (#/artwork, same). All three
         * pointed at DOM that no longer renders, so they toured nothing. */
        'library': [
            { target: null, title: 'Your Library', body: 'Every work you’ve made — stories and artwork alike — on one shelf, with views, favourites and comments pulled in from every platform it’s live on.' },
            { target: '.shelf-segs', title: 'Filter by type', body: 'All, Stories, Artwork, or Discovered. Each piece is one card; press the button on its picture to see its other versions, or a story’s chapters. <em>Discovered</em> is what polling found that isn’t in your library yet.' },
            { target: '.shelf-discovered-banner', title: 'Discovered art', body: 'When polling finds art on your accounts that isn’t here yet, this offers to import the lot in one go.' },
            { target: '#shelf-search', title: 'Search the shelf', body: 'Type a title to narrow the shelf instantly — handy once you’re tracking a lot of pieces.' },
            { target: '.shelf-sort', title: 'Sort', body: 'Newest, A–Z, most platforms — or by pooled views, favourites and comments to see what’s actually landing.' },
            { target: '#shelf-grid', title: 'Your works', body: 'Each cover tells the truth: a gilt ribbon means it’s live, and it says on how many platforms. Click one for its full per-platform detail.' },
            { target: '.empty-state', title: 'Get works in', body: 'Nothing here yet? Add art with <strong>New Artwork</strong> or a story with <strong>New Story</strong> in the menu — or connect a site and PawPoller finds what you’ve already posted.' },
        ],

        'queue': [
            { target: null, title: 'The posting queue', body: 'The Queue holds every upload and update that’s pending, scheduled or being processed right now.' },
            { target: '.page-header', title: 'Posting Queue', body: 'Your work-in-progress list — items sit here until the scheduler runs them, then move on to History.' },
            { target: '.empty-state', title: 'Nothing queued', body: 'The Queue starts empty. Publish or schedule a story or a piece of art and its jobs appear here.' },
            { target: '.data-table', title: 'Queued items', body: 'Each row shows the story, platform, action and status. Pending items get a <em>Cancel</em> button before they run.' },
            { target: '.nav-link[data-page="posting-log"]', title: 'History', body: 'Once a queued item finishes, its outcome is recorded over in History.' },
        ],

        'history': [
            { target: null, title: 'Your posting log', body: 'History is the audit trail of every publish and update PawPoller has run for you.' },
            { target: '.page-header', title: 'Posting History', body: 'A record of what was posted where, whether it succeeded, and how long each job took.' },
            { target: '.empty-state', title: 'No activity yet', body: 'Once you publish or update a story, each attempt is logged here — successes and failures alike.' },
            { target: '.data-table', title: 'Log entries', body: 'Every row lists the time, story, platform, action and result, with a link to the post and its duration.' },
            { target: '.nav-link[data-page="posting-queue"]', title: 'Queue', body: 'Work still in progress lives in the Queue; it only lands here once it has run.' },
        ],

        'editor': [
            { target: null, title: 'Story Editor', body: 'This is where you write and manage your stories. Let’s take a quick look around.' },
            { target: '.page-header', title: 'Story Editor', body: 'Your writing hub — every story lives here as a <em>MASTER.md</em> you can edit and preview in all publishing formats.' },
            { target: '#create-story-btn', title: 'Create a story', body: 'Start a fresh story from a blank template — set the title, chapters and rating up front.' },
            { target: '#import-story-btn', title: 'Import a story', body: 'Pull in a story you’ve already posted — paste a URL or ID, or pick one from your polled platforms.' },
            { target: '#regen-all-btn', title: 'Regenerate all', body: 'Rebuild every story’s derived formats (BBCode, HTML, EPUB and more) from its MASTER.md in one go.' },
            { target: '.card-grid', title: 'Your stories', body: 'Each story appears here as a card you can open. A new account starts empty — create or import your first.' },
        ],

        'posts': [
            { target: null, title: 'Welcome to Posts', body: 'Your short posts and journals, across every account they went to.' },
            { target: '.pp-new-btn', title: 'Write something', body: '<em>New post</em> for a short update with pictures; <em>New journal</em> for a titled journal on Weasyl, DeviantArt and FurAffinity.' },
            { target: '#pp-status', title: 'Filter', body: 'Everything, only what’s scheduled, only what failed (with the reason and a retry), or only journals.' },
            { target: '#post-feed', title: 'Your posts', body: 'Each post shows where it went and how it did. ⋯ posts it again, copies the text or adds it to a collection.' },
        ],
        'posts-new': [
            { target: null, title: 'New post', body: 'Write a short update once and send it to all your accounts in one go.' },
            { target: '#post-body', title: 'Write your post', body: 'Type it here. Each site’s ring fills as you near its length limit (Bluesky’s is 300 characters).' },
            { target: 'label.pp-tool:has(#post-image)', title: 'Pictures', body: 'Add up to four pictures with alt text. Several pictures go up together where the site allows it.' },
            { target: '#post-rating', title: 'Rating', body: 'General, Mature or Adult. Sites that don’t allow a rating grey themselves out and say why.' },
            { target: '#post-platforms', title: 'Where it goes', body: 'Switch on the accounts to post to. A site that can’t take this post tells you why instead.' },
            { target: '#post-submit', title: 'Post or schedule', body: '<em>Post now</em> sends it to every switched-on site; <em>Schedule…</em> picks a time, with your best time to post suggested.' },
        ],

        'analytics': [
            { target: null, title: 'Analytics', body: 'This page tracks how your work grows over time. Let’s take a quick lap of the highlights and exports.' },
            { target: '.page-header', title: 'Your growth overview', body: 'All-time trends across every platform combined — Best Month highlights, fastest-growing works and a 12-week chart.' },
            { target: '.stats-grid', title: 'Best Month cards', body: 'Your biggest single month for views, favourites and comments — each card shows the gain and when it happened.' },
            { target: '#analytics-export-fastest', title: 'Export to CSV', body: 'Grab the fastest-growing and weekly-growth tables as spreadsheet-ready CSV files whenever there’s data.' },
            { target: '#analytics-export-chart', title: 'Save the chart', body: 'Download the 12-week growth chart as a PNG — handy for sharing or dropping into a report.' },
        ],

        'groups': [
            { target: null, title: 'Submission Groups', body: 'Groups bundle submissions from any platform together so you can track their combined stats in one place.' },
            { target: '.page-header', title: 'Groups overview', body: 'This is your Groups page. Every group you create for combined tracking is listed right here.' },
            { target: '#create-group-btn', title: 'Create a group', body: 'Start here — give your group a name and description, then add submissions to it from any platform.' },
            { target: '.stats-grid', title: 'Your groups', body: 'Each group shows as a card in this area; click one to open it and manage its members and running totals.' },
            { target: '.empty-state', title: 'Nothing here yet', body: 'While you have no groups, this prompt sits here — it disappears the moment you create your first one.' },
        ],

        'collections': [
            { target: null, title: 'Collections', body: 'A collection is one master folder per piece — every place it is posted across platforms, with pooled analytics, merged tags and an optional companion story.' },
            { target: '.page-header', title: 'Collections overview', body: 'This is your Collections hub. Each piece you group across platforms shows here as a card with its combined figures.' },
            { target: '[data-coll-new]', title: 'New collection', body: 'Start one here, then add works, submissions or a story — or use “Add to Collection” from the Submissions hub.' },
            { target: '#coll-suggest', title: 'Suggested collections', body: 'PawPoller spots the same piece posted to several platforms and offers to merge them into a collection in one click.' },
        ],

        'accounts': [
            { target: null, title: 'Your accounts', body: 'Manage every identity you post as — the accounts and personas behind your stories, artwork and posts.' },
            { target: '#personas-card', title: 'Personas', body: 'Bundle accounts across platforms into one <em>persona</em> for scoped views and per-persona digests.' },
            { target: '#accounts-add', title: 'Add an account', body: 'Pick a platform, label the account and enter its credentials. The first on a platform becomes its default.' },
            { target: '#accounts-list', title: 'Accounts by platform', body: 'Your accounts live here, grouped by platform — toggle, rename, delete or assign each to a persona.' },
            { target: '#fa-polling-card', title: 'FurAffinity polling', body: 'Choose how PawPoller checks FurAffinity for new favourites, comments and watchers.' },
        ],

        'settings': [
            { target: null, title: 'Welcome to Settings', body: 'This is where PawPoller is configured — connect platforms, tune polling, secure your dashboard and more.' },
            { target: '#settings-rail', title: 'Settings pages', body: 'Every settings page is down this rail. Search at the top finds a setting on any page.' },
            { target: '[data-spage="preferences"]', title: 'General', body: 'Your time zone lives here. Every time on screen, every schedule and every chart uses it.' },
            { target: '[data-spage="platforms"]', title: 'Platforms', body: 'Connect each site you post to (FurAffinity, Inkbunny, Bluesky and the rest), each with a step-by-step guide.' },
            { target: '[data-spage="notifications"]', title: 'Notifications', body: 'Every site\'s desktop-alert switch in one table, then Telegram, Discord and the weekly email. Everything in Settings saves the moment you change it.' },
            { target: '[data-spage="polling"]', title: 'Polling', body: 'Set how often each platform is checked for new favourites and comments, and pause or resume polling.' },
            { target: '[data-spage="security"]', title: 'Security', body: 'Lock down your dashboard — change your password, turn on two-factor login and manage API keys.' },
        ],
    };

    /* Map a location hash to a tour name (or null for routes with no tour —
     * full-screen login/loading/setup, platform sub-pages, deep detail views). */
    function tourForHash(hash) {
        const h = (hash != null ? hash : location.hash || '').replace(/^#\/?/, '');
        const parts = h.split('/').filter(Boolean);
        const p0 = parts[0] || '';
        if (!p0 || p0 === 'overview') return GS;
        if (p0 === 'platforms') return 'platforms';
        // The one works hub — bare #/library and any #/library/type/{segment}.
        // Not /work/ or /sort/ (deep detail / a pre-sorted deep-link).
        if (p0 === 'library' && (!parts[1] || parts[1] === 'type')) return 'library';
        if (p0 === 'analytics') return 'analytics';
        if (p0 === 'groups' && !parts[1]) return 'groups';
        if (p0 === 'collections' && !parts[1]) return 'collections';
        if (p0 === 'accounts' && !parts[1]) return 'accounts';
        if (p0 === 'settings') return 'settings';
        if (p0 === 'posts') {
            if (parts[1] === 'new') return 'posts-new';   // the composer moved to Create
            if (!parts[1]) return 'posts';
            return null;                                    // contacts etc. — no tour
        }
        if (p0 === 'editor' && !parts[1]) return 'editor';
        if (p0 === 'posting') {
            // Bare #/posting and #/artwork redirect into Library, so they never
            // reach here — only these sub-pages still have tours of their own.
            if (parts[1] === 'queue') return 'queue';
            if (parts[1] === 'log') return 'history';
        }
        return null;
    }

    // ── Engine state ──
    let _name = null, _steps = [], _idx = 0, _dir = 1;
    let _running = false, _lastEndAt = 0;
    let _blocker = null, _spot = null, _pop = null;
    let _prevSidebar = null, _onResize = null, _onKey = null;

    function el(tag, cls) { const n = document.createElement(tag); n.className = cls; return n; }
    function isMobile() { return document.documentElement.dataset.mobile === '1'; }
    function isVisible(node) {
        if (!node) return false;
        const r = node.getBoundingClientRect();
        return r.width > 0 || r.height > 0;
    }

    function forceSidebarOpen() {
        const sb = document.querySelector('.sidebar');
        if (!sb) return;
        _prevSidebar = { collapsed: sb.classList.contains('collapsed'), open: sb.classList.contains('open') };
        sb.classList.remove('collapsed');
        sb.classList.add('open');
        document.getElementById('sidebar-overlay')?.classList.remove('open');
    }
    function restoreSidebar() {
        const sb = document.querySelector('.sidebar');
        if (!sb || !_prevSidebar) return;
        sb.classList.toggle('collapsed', _prevSidebar.collapsed);
        sb.classList.toggle('open', _prevSidebar.open);
        _prevSidebar = null;
    }

    /* Resolve a selector to a VISIBLE element, retrying briefly while the page
     * renders. Returns null if it never appears / stays hidden — the caller
     * then skips that step. */
    async function findTarget(sel, tries) {
        if (!sel) return null;
        tries = tries || 12;
        for (let i = 0; i < tries; i++) {
            const t = document.querySelector(sel);
            if (t && isVisible(t)) return t;
            await new Promise(r => setTimeout(r, 60));
        }
        return null;
    }

    function renderPop(step) {
        const n = _idx + 1, total = _steps.length;
        const isFirst = _idx === 0, isLast = _idx === total - 1;
        _pop.innerHTML =
            '<div class="pp-tour-pop-head">'
                + '<span class="pp-tour-step">' + n + ' / ' + total + '</span>'
                + '<button class="pp-tour-x" type="button" aria-label="Close tour">&times;</button>'
            + '</div>'
            + '<div class="pp-tour-pop-title">' + step.title + '</div>'
            + '<div class="pp-tour-pop-body">' + step.body + '</div>'
            + '<div class="pp-tour-pop-foot">'
                + (isLast ? '<span></span>' : '<button class="pp-tour-skip" type="button">Skip</button>')
                + '<div class="pp-tour-nav">'
                    + (isFirst ? '' : '<button class="pp-tour-back" type="button">Back</button>')
                    + '<button class="pp-tour-next" type="button">' + (isLast ? (step.cta || 'Finish') : 'Next') + '</button>'
                + '</div>'
            + '</div>';
        _pop.querySelector('.pp-tour-x').onclick = () => end(false);
        const skip = _pop.querySelector('.pp-tour-skip'); if (skip) skip.onclick = () => end(false);
        const back = _pop.querySelector('.pp-tour-back'); if (back) back.onclick = () => prev();
        _pop.querySelector('.pp-tour-next').onclick = () => (isLast ? end(true) : next());
        _pop.querySelectorAll('.pp-tour-link').forEach(a => a.addEventListener('click', () => end(true)));
    }

    function positionPop(r) {
        const vw = window.innerWidth, vh = window.innerHeight;
        const pw = _pop.offsetWidth, ph = _pop.offsetHeight, gap = 14;
        let left, top;
        if (isMobile()) {
            left = Math.max(12, (vw - pw) / 2);
            top = Math.max(12, vh - ph - 88);          // clear the bottom nav
        } else if (r.right < vw * 0.45) {
            left = r.right + gap;                        // left-side target → popover to its right
            top = Math.min(Math.max(8, r.top), vh - ph - 8);
        } else if (vh - r.bottom > ph + gap) {
            top = r.bottom + gap;
            left = Math.min(Math.max(8, r.left + r.width / 2 - pw / 2), vw - pw - 8);
        } else {
            top = Math.max(8, r.top - ph - gap);
            left = Math.min(Math.max(8, r.left + r.width / 2 - pw / 2), vw - pw - 8);
        }
        _pop.style.left = left + 'px';
        _pop.style.top = top + 'px';
    }

    function position(target) {
        if (!_pop) return;
        const step = _steps[_idx];
        target = target || (step && step.target ? document.querySelector(step.target) : null);
        if (target && !isVisible(target)) target = null;
        if (!target) {
            _spot.style.display = 'none';
            _blocker.classList.add('pp-tour-blocker--dim');
            _pop.classList.add('pp-tour-pop--center');
            _pop.style.transform = 'translate(-50%, -50%)';
            _pop.style.left = '50%';
            _pop.style.top = '50%';
            return;
        }
        _spot.style.display = 'block';
        _blocker.classList.remove('pp-tour-blocker--dim');
        _pop.classList.remove('pp-tour-pop--center');
        _pop.style.transform = '';
        const r = target.getBoundingClientRect();
        const pad = 6;
        _spot.style.left = Math.max(0, r.left - pad) + 'px';
        _spot.style.top = Math.max(0, r.top - pad) + 'px';
        _spot.style.width = (r.width + pad * 2) + 'px';
        _spot.style.height = (r.height + pad * 2) + 'px';
        positionPop(r);
    }

    async function show(i) {
        if (!_running) return;
        if (i < 0) i = 0;
        if (i >= _steps.length) { end(true); return; }
        _idx = i;
        const step = _steps[_idx];
        if (step.route && location.hash.replace(/^#\/?/, '') !== step.route.replace(/^#\/?/, '')) {
            location.hash = step.route;
            await new Promise(r => setTimeout(r, 250));
            if (!_running) return;
        }
        let target = null;
        if (step.target) {
            target = await findTarget(step.target);
            if (!_running) return;
            if (!target) {
                // Skip a missing/hidden step in the current direction.
                const j = _idx + _dir;
                if (j >= 0 && j < _steps.length) return show(j);
                if (_dir > 0) { end(true); return; }      // ran off the end going forward
                target = null;                             // ran off the start going back → centre it
            }
        }
        if (target && target.scrollIntoView) target.scrollIntoView({ block: 'nearest' });
        renderPop(step);
        position(target);
    }

    function next() { _dir = 1; if (_idx < _steps.length - 1) show(_idx + 1); else end(true); }
    function prev() { _dir = -1; if (_idx > 0) show(_idx - 1); }

    function begin(name, steps, opts) {
        if (_running) return;
        _running = true;
        _name = name;
        _steps = steps;
        _idx = 0;
        _dir = 1;
        forceSidebarOpen();
        document.documentElement.classList.add('pp-tour-active');

        _blocker = el('div', 'pp-tour-blocker');
        _spot = el('div', 'pp-tour-spot');
        _pop = el('div', 'pp-tour-pop');
        _blocker.addEventListener('click', (e) => e.stopPropagation());
        document.body.appendChild(_blocker);
        document.body.appendChild(_spot);
        document.body.appendChild(_pop);

        _onResize = () => position();
        window.addEventListener('resize', _onResize);
        window.addEventListener('scroll', _onResize, true);
        _onKey = (e) => {
            if (e.key === 'Escape') end(false);
            else if (e.key === 'ArrowRight') next();
            else if (e.key === 'ArrowLeft') prev();
        };
        document.addEventListener('keydown', _onKey);

        show(0);
    }

    function end(completed) {
        if (!_running) return;
        _running = false;
        _lastEndAt = Date.now();
        // Persist "seen" locally (instant) AND to the server (durable across
        // browsers/PWA/updates). Whether the user finished or dismissed, the
        // tour shouldn't auto-offer again.
        const seenName = _name;
        markLocal(seenName);
        if (_serverSeen) _serverSeen.add(seenName);
        postSeen(seenName);
        if (_onResize) {
            window.removeEventListener('resize', _onResize);
            window.removeEventListener('scroll', _onResize, true);
            _onResize = null;
        }
        if (_onKey) { document.removeEventListener('keydown', _onKey); _onKey = null; }
        [_blocker, _spot, _pop].forEach(n => n && n.remove());
        _blocker = _spot = _pop = null;
        document.documentElement.classList.remove('pp-tour-active');
        restoreSidebar();
        _name = null; _steps = []; _idx = 0; _dir = 1;
    }

    function isDone(name) {
        if (_serverSeen && _serverSeen.has(name)) return true;
        return localDone(name);
    }

    function start(name, opts) {
        if (_running) return;
        const steps = TOURS[name];
        if (!steps || !steps.length) return;
        begin(name, steps, opts || {});
    }

    function startHere(opts) {
        start(tourForHash(location.hash) || GS, opts);
    }

    /* Auto-fire hook, called from App.route() with the hash at dispatch time. */
    async function maybeAuto(hash) {
        try {
            if (_running) return;
            const name = tourForHash(hash);
            if (!name) return;
            const steps = TOURS[name];
            if (!steps || !steps.length) return;
            // Load the server-side seen set before deciding, so a tour the user
            // already dismissed on another browser/the PWA never re-fires here.
            await hydrate();
            if (_running) return;                          // a manual tour may have started while we awaited
            if (isDone(name)) return;
            if (name !== GS) {
                if (!isDone(GS)) return;                       // page tours wait for getting-started
                if (Date.now() - _lastEndAt < 1200) return;   // don't chain straight after another tour
            }
            if (tourForHash(location.hash) !== name) return;  // still on the page we were called for?
            // Wait for the first *targeted* element so we don't fire over a half-rendered page.
            const firstTarget = (steps.find(s => s.target) || {}).target;
            if (firstTarget && !(await findTarget(firstTarget, 30))) return;
            if (_running || isDone(name) || tourForHash(location.hash) !== name) return;
            begin(name, steps, { auto: true });
        } catch (e) { /* never let onboarding break navigation */ }
    }

    /* Mark a tour seen without running it (setup's "Go to the dashboard", spec 032). */
    function skip(name) {
        markLocal(name);
        if (_serverSeen) _serverSeen.add(name);
        postSeen(name);
    }

    return { start, startHere, maybeAuto, end, isDone, hydrate, tourForHash, skip };
})();
