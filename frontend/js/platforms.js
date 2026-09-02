/* ===========================================================================
 * PawPoller — canonical platform registry
 * ---------------------------------------------------------------------------
 * Single source of truth for the 17 platforms. Loaded FIRST (before every
 * other frontend script) so the command palette, shell, Platforms hub and the
 * context-bar switcher all read `window.PLATFORMS` instead of re-declaring the
 * list (it used to be hand-duplicated in 5 places). Brand colours are the
 * theme-invariant `--platform-*` tokens from tokens.css.
 * ===========================================================================*/
(function () {
    const PLATFORMS = [
        { code: 'ib',   label: 'Inkbunny',     emoji: '\u{1F43E}', color: 'var(--platform-ib)',   pollOnly: false },
        { code: 'fa',   label: 'FurAffinity',  emoji: '\u{1F98A}', color: 'var(--platform-fa)',   pollOnly: false },
        { code: 'ws',   label: 'Weasyl',       emoji: '\u{1F98E}', color: 'var(--platform-ws)',   pollOnly: false },
        { code: 'sf',   label: 'SoFurry',      emoji: '\u{1F4DC}', color: 'var(--platform-sf)',   pollOnly: false },
        { code: 'sqw',  label: 'SquidgeWorld', emoji: '\u{1F999}', color: 'var(--platform-sqw)',  pollOnly: false },
        { code: 'ao3',  label: 'AO3',          emoji: '\u{1F4D6}', color: 'var(--platform-ao3)',  pollOnly: false },
        { code: 'da',   label: 'DeviantArt',   emoji: '\u{1F3A8}', color: 'var(--platform-da)',   pollOnly: false },
        { code: 'wp',   label: 'Wattpad',      emoji: '\u{1F4D3}', color: 'var(--platform-wp)',   pollOnly: true  },
        { code: 'ik',   label: 'Itaku',        emoji: '\u{1F5BC}', color: 'var(--platform-ik)',   pollOnly: true  },
        { code: 'bsky', label: 'Bluesky',      emoji: '\u{1F98B}', color: 'var(--platform-bsky)', pollOnly: true  },
        { code: 'tw',   label: 'X / Twitter',  emoji: '\u{1F426}', color: 'var(--platform-tw)',   pollOnly: true  },
        { code: 'mast', label: 'Mastodon',     emoji: '\u{1F418}', color: 'var(--platform-mast)', pollOnly: true  },
        { code: 'tum',  label: 'Tumblr',       emoji: '\u{1F4D8}', color: 'var(--platform-tum)',  pollOnly: true  },
        { code: 'pix',  label: 'Pixiv',        emoji: '\u{1F58C}', color: 'var(--platform-pix)',  pollOnly: true  },
        { code: 'thr',  label: 'Threads',      emoji: '\u{1F9F5}', color: 'var(--platform-thr)',  pollOnly: true  },
        { code: 'ig',   label: 'Instagram',    emoji: '\u{1F4F8}', color: 'var(--platform-ig)',   pollOnly: true  },
        { code: 'e621', label: 'e621',         emoji: '\u{1F43E}', color: 'var(--platform-e621)', pollOnly: false },
        { code: 'fn',   label: 'FurryNetwork', emoji: '\u{1F310}', color: '#3b8ed0',               pollOnly: false },
        { code: 'fbr',  label: 'Furbooru',     emoji: '\u{1F5BC}', color: '#3d7b3d',               pollOnly: true  },
    ];

    /* ── Metric metadata ──────────────────────────────────────────────────
     * Mirrors database/platform_metrics.py — same codes, same families, same
     * API keys. Kept in step by tests/test_platform_metrics.py, which fails if
     * the two registries list different platforms.
     *
     * `statKey` names the field each metric arrives under in the per-platform
     * `/api/{code}/stats` payload. Before this, app.js hand-wrote those field
     * names in four separate blocks (the totals sum, the platform breakdown
     * grid, the roll-up and the chart list) — all four had to be edited to add
     * a platform, so FurryNetwork and Furbooru appeared in none of them.
     *
     * family: 'views'      has a real view/read counter
     *         'score'      headline metric is a net up−down score (may be
     *                      NEGATIVE) — never summed into a view total
     *         'engagement' no view counter at all; likes/notes only
     *
     * A null statKey means the platform does not report that metric. `labels`
     * carries the platform's own vocabulary (AO3 "Hits", Tumblr "Notes") for
     * display; it is never used as a data key.
     *
     * `snap` is the matching column in that platform's SNAPSHOT rows (what the
     * trend charts plot), and `snapKey` says which canonical metric it is so
     * the chart can be titled in the platform's own words. */
    const M = (family, views, faves, comments, snap, snapKey, labels) => ({
        family,
        views: views || null,
        faves: faves || null,
        comments: comments || null,
        score: family === 'score' ? 'total_score' : null,
        snap: snap,
        snapKey: snapKey,
        labels: labels || {},
    });
    const V = (labels) => M('views', 'total_views', 'total_favorites', 'total_comments',
        'views', 'views', labels);
    const METRICS = {
        ib:   V(),
        fa:   V(),
        ws:   V(),
        sf:   V(),
        // OTW archives: the SITE says hits/kudos, the payload says views/favorites.
        sqw:  V({ views: 'Hits', faves: 'Kudos' }),
        ao3:  V({ views: 'Hits', faves: 'Kudos' }),
        da:   V(),
        pix:  V(),
        fn:   V(),
        wp:   M('views', 'total_reads', 'total_votes', 'total_comments',
                'reads', 'views', { views: 'Reads', faves: 'Votes' }),
        ik:   M('engagement', null, 'total_likes', 'total_comments',
                'likes', 'faves', { faves: 'Likes' }),
        bsky: M('engagement', null, 'total_likes', 'total_replies',
                'likes', 'faves', { faves: 'Likes', comments: 'Replies' }),
        tw:   M('views', 'total_views', 'total_likes', 'total_comments',
                'views', 'views', { faves: 'Likes' }),
        mast: M('engagement', null, 'total_likes', 'total_replies',
                'likes', 'faves', { faves: 'Likes', comments: 'Replies' }),
        tum:  M('engagement', null, 'total_notes', null,
                'notes', 'faves', { faves: 'Notes' }),
        thr:  M('views', 'total_views', 'total_likes', 'total_replies',
                'views', 'views', { faves: 'Likes', comments: 'Replies' }),
        ig:   M('views', 'total_views', 'total_likes', 'total_comments',
                'views', 'views', { faves: 'Likes' }),
        e621: M('score', null, 'total_favorites', 'total_comments', 'score', 'score'),
        fbr:  M('score', null, 'total_favorites', 'total_comments', 'score', 'score'),
    };
    PLATFORMS.forEach(p => { p.metrics = METRICS[p.code] || V(); });

    // Display order is alphabetical by label (case-insensitive) everywhere that
    // reads window.PLATFORMS — the Platforms hub, command palette, context-bar
    // switcher and Overview tiles. Sort once here so all consumers agree.
    PLATFORMS.sort((a, b) => a.label.toLowerCase().localeCompare(b.label.toLowerCase()));

    const byCode = {};
    PLATFORMS.forEach(p => { byCode[p.code] = p; });

    // Each platform's official logo, bundled under /img/platforms/. Itaku and
    // Weasyl ship SVGs (scalable); the rest are PNGs. Trademarks of their owners
    // — see the disclaimer on the Platforms hub.
    const _svgLogos = ['ik', 'ws', 'mast', 'tum', 'pix', 'thr', 'ig', 'e621'];
    // Platforms with no bundled logo asset fall back to their emoji (the tile
    // renderer treats a null logo that way). Keeps a broken <img> off the hub.
    const _noLogo = ['fn', 'fbr'];
    PLATFORMS.forEach(p => {
        p.logo = _noLogo.includes(p.code)
            ? null
            : '/img/platforms/' + p.code + (_svgLogos.includes(p.code) ? '.svg' : '.png');
    });

    /* platformRoute(code, sub) — hash route for a platform sub-view.
     *
     * Every platform (including Inkbunny, as of 2.68.0) is uniform:
     * `#/{code}`, `#/{code}/submissions`, `#/{code}/compare`,
     * `#/{code}/submission/{id}`. The top-level `#/submissions` is now the
     * cross-platform Submissions hub, not IB's table.
     *
     *   sub: undefined → dashboard, 'submissions', or 'compare'
     */
    function platformRoute(code, sub) {
        if (!sub) return '#/' + code;
        return '#/' + code + '/' + sub;
    }

    /* platformStat(code, stats, key) — read one canonical metric out of a
     * platform's /api/{code}/stats payload.
     *
     * key is 'subs' | 'views' | 'score' | 'faves' | 'comments'. Returns 0 when
     * the platform doesn't report that metric, so callers can sum blindly.
     * This is the function that replaced four hand-written blocks of
     * `(ik.total_likes || 0) + (bsky.total_likes || 0) + …` in app.js. */
    function platformStat(code, stats, key) {
        if (!stats) return 0;
        if (key === 'subs') return stats.total_submissions || 0;
        const m = (byCode[code] && byCode[code].metrics) || null;
        if (!m) return 0;
        const field = m[key];
        return field ? (stats[field] || 0) : 0;
    }

    /* platformMetricLabel(code, key) — the platform's own word for a metric
     * ("Hits" on AO3, "Notes" on Tumblr). Display only. */
    function platformMetricLabel(code, key) {
        const DEFAULTS = { views: 'Views', score: 'Score', faves: 'Favourites', comments: 'Comments' };
        const m = (byCode[code] && byCode[code].metrics) || null;
        return (m && m.labels && m.labels[key]) || DEFAULTS[key] || key;
    }

    /* visiblePlatforms(exclude) — the platform list a widget should render.
     *
     * `exclude` is an array of codes the user has switched OFF for that widget.
     * Storing EXCLUSIONS (rather than inclusions) is deliberate: a platform
     * connected later shows up everywhere by default, so a filtered widget can
     * never quietly under-count new work. */
    function visiblePlatforms(exclude) {
        if (!Array.isArray(exclude) || !exclude.length) return PLATFORMS;
        const off = new Set(exclude);
        return PLATFORMS.filter(p => !off.has(p.code));
    }

    /* isPlatformVisible(code, exclude) — the same test for a single code, for
     * widgets that filter rows of data rather than the platform list. */
    function isPlatformVisible(code, exclude) {
        return !(Array.isArray(exclude) && exclude.includes(code));
    }

    window.PLATFORMS = PLATFORMS;
    window.platformByCode = function (code) { return byCode[code] || null; };
    window.platformRoute = platformRoute;
    window.platformStat = platformStat;
    window.platformMetricLabel = platformMetricLabel;
    window.visiblePlatforms = visiblePlatforms;
    window.isPlatformVisible = isPlatformVisible;
})();
