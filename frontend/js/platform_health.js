/* PawPoller platform health status — single source for the sidebar
 * grid dots, per-platform "last polled · next in" subtitles, and
 * throttle banners.
 *
 * Polls /api/platforms/health every 60s. One fetch fans out across
 * every UI surface that needs the same data — components subscribe
 * via PlatformHealth.subscribe(fn) instead of fanning out their own
 * HTTP requests.
 *
 * Health states (worst → best):
 *   error        — last poll status was 'error'
 *   throttled    — known throttle window active (currently AO3 only)
 *   stale        — no poll in 2× the configured interval
 *   running      — a poll is currently in progress
 *   healthy      — last poll succeeded recently
 *   unconfigured — no credentials present
 *   unknown      — never polled or status couldn't be determined
 */

(function () {
    const POLL_INTERVAL_MS = 60_000;
    const PLATFORMS = ['ib', 'fa', 'ws', 'sf', 'sqw', 'ao3', 'da', 'wp', 'ik', 'bsky', 'tw', 'mast', 'tum', 'pix', 'thr', 'ig', 'e621'];
    const LABELS = {
        ib: 'Inkbunny', fa: 'FurAffinity', ws: 'Weasyl', sf: 'SoFurry',
        sqw: 'SquidgeWorld', ao3: 'AO3', da: 'DeviantArt', wp: 'Wattpad',
        ik: 'Itaku', bsky: 'Bluesky', tw: 'X/Twitter', mast: 'Mastodon', tum: 'Tumblr', pix: 'Pixiv', thr: 'Threads', ig: 'Instagram', e621: 'e621',
    };

    let _data = {};
    let _timer = null;
    const _listeners = new Set();

    function classify(entry) {
        if (!entry || !entry.configured) return 'unconfigured';
        if (entry.throttled_until) return 'throttled';
        // A 'partial' poll = the last cycle was rate-limited (429 / block) and
        // returned incomplete data — surface it as the amber throttled state.
        if (entry.last_poll_status === 'partial') return 'throttled';
        if (entry.last_poll_status === 'error') return 'error';
        if (entry.last_poll_status === 'running') return 'running';
        if (!entry.last_poll_at) return 'unknown';
        const last = new Date(entry.last_poll_at).getTime();
        if (isNaN(last)) return 'unknown';
        const intervalMs = (entry.interval_minutes || 60) * 60 * 1000;
        if (Date.now() - last > intervalMs * 2) return 'stale';
        return 'healthy';
    }

    function relativePast(iso) {
        if (!iso) return 'never';
        const t = new Date(iso).getTime();
        if (isNaN(t)) return 'never';
        const seconds = Math.max(0, Math.round((Date.now() - t) / 1000));
        if (seconds < 60) return `${seconds}s ago`;
        const minutes = Math.round(seconds / 60);
        if (minutes < 60) return `${minutes}m ago`;
        const hours = Math.round(minutes / 60);
        if (hours < 24) return `${hours}h ago`;
        const days = Math.round(hours / 24);
        return `${days}d ago`;
    }

    function relativeFuture(iso) {
        if (!iso) return null;
        const t = new Date(iso).getTime();
        if (isNaN(t)) return null;
        const delta = Math.round((t - Date.now()) / 1000);
        if (delta <= 0) return 'now';
        if (delta < 60) return `${delta}s`;
        const minutes = Math.round(delta / 60);
        if (minutes < 60) return `${minutes}m`;
        const hours = Math.round(minutes / 60);
        return `${hours}h`;
    }

    function tooltipFor(code, entry) {
        const label = LABELS[code] || code.toUpperCase();
        if (!entry) return `${label}: status unknown`;
        if (!entry.configured) return `${label}: not configured`;
        const state = classify(entry);
        const lines = [`${label}: ${state}`];
        if (entry.last_poll_at) lines.push(`Last polled ${relativePast(entry.last_poll_at)}`);
        if (entry.next_poll_at && state !== 'throttled') {
            const inFuture = relativeFuture(entry.next_poll_at);
            if (inFuture) lines.push(`Next poll in ${inFuture}`);
        }
        if (entry.throttled_until) {
            const remaining = relativeFuture(entry.throttled_until);
            if (remaining) lines.push(`Throttled — resumes in ${remaining}`);
        }
        if (entry.last_poll_error) {
            lines.push(`Last error: ${entry.last_poll_error.slice(0, 120)}`);
        }
        return lines.join('\n');
    }

    function renderDots() {
        for (const code of PLATFORMS) {
            const el = document.getElementById(`pg-status-${code}`);
            if (!el) continue;
            const entry = _data[code];
            const state = classify(entry);
            // Preserve the original platform-grid-status class so the
            // grid-item layout (margin-left: auto pushing the dot to
            // the right edge of the card) keeps working; layer the
            // health classes on top.
            el.className = `platform-grid-status pp-health-dot pp-health-${state}`;
            el.setAttribute('data-tooltip', tooltipFor(code, entry));
        }
    }

    // Detect the current platform from the URL hash. Matches #/sf,
    // #/ao3, etc. — top-level platform dashboard pages only. Returns
    // null on Overview / Settings / per-submission pages so we don't
    // try to inject a status subtitle into headers that aren't about
    // a single platform.
    function currentPlatformFromHash() {
        const hash = (window.location.hash || '').replace(/^#\//, '');
        const code = hash.split(/[/?]/)[0].toLowerCase();
        return PLATFORMS.includes(code) ? code : null;
    }

    function subtitleText(entry) {
        if (!entry) return '';
        if (!entry.configured) return 'Not configured';
        const parts = [];
        if (entry.last_poll_at) {
            parts.push(`Last polled ${relativePast(entry.last_poll_at)}`);
        } else {
            parts.push('Never polled');
        }
        if (entry.throttled_until) {
            const remaining = relativeFuture(entry.throttled_until);
            parts.push(`throttled${remaining ? ` ${remaining} remaining` : ''}`);
        } else if (entry.last_poll_status === 'partial') {
            parts.push('⚠ throttled — data may be incomplete');
        } else if (entry.next_poll_at) {
            const inFuture = relativeFuture(entry.next_poll_at);
            if (inFuture) parts.push(`next in ${inFuture}`);
        }
        if (entry.last_poll_status === 'error') {
            parts.push('last poll failed');
        }
        return parts.join(' · ');
    }

    function renderPageSubtitle() {
        const code = currentPlatformFromHash();
        if (!code) return;
        // Each platform dashboard's header is the first .page-header
        // inside #app. Skip on pages that don't render one (e.g.
        // _loading() placeholder during async data fetches).
        const headerH2 = document.querySelector('#app .page-header h2');
        if (!headerH2) return;
        let subtitle = headerH2.parentElement.querySelector('.platform-page-subtitle');
        if (!subtitle) {
            subtitle = document.createElement('div');
            subtitle.className = 'platform-page-subtitle';
            headerH2.insertAdjacentElement('afterend', subtitle);
        }
        const entry = _data[code];
        subtitle.textContent = subtitleText(entry);
        // Tooltip with full detail (error message, etc.)
        if (entry && (entry.last_poll_error || entry.throttled_until)) {
            subtitle.setAttribute('data-tooltip', tooltipFor(code, entry));
            subtitle.classList.add('has-issue');
        } else {
            subtitle.removeAttribute('data-tooltip');
            subtitle.classList.remove('has-issue');
        }
        renderPageBanner(code, entry);
    }

    function bannerContent(code, entry) {
        if (!entry) return null;
        const label = LABELS[code] || code.toUpperCase();
        if (entry.throttled_until) {
            const remaining = relativeFuture(entry.throttled_until);
            const at = new Date(entry.throttled_until).toLocaleTimeString([], {
                hour: '2-digit', minute: '2-digit',
            });
            return {
                kind: 'throttled',
                title: `${label} is throttled`,
                body: `Resumes at ${at}${remaining ? ` (${remaining} remaining)` : ''}. Polls and posts to this platform are paused until then.`,
                action: null,
            };
        }
        if (entry.last_poll_status === 'partial') {
            return {
                kind: 'throttled',
                title: `${label}: last poll was throttled`,
                body: (entry.last_poll_error ? entry.last_poll_error.slice(0, 240) + ' ' : '')
                    + 'Rate-limited — some data may be incomplete. It fills in on the next un-throttled poll; avoid forcing repeated resyncs (that keeps the throttle alive).',
                action: null,
            };
        }
        if (entry.last_poll_status === 'error') {
            return {
                kind: 'error',
                title: `${label}: last poll failed`,
                body: entry.last_poll_error
                    ? entry.last_poll_error.slice(0, 240)
                    : 'No error message recorded — check the poll log for detail.',
                // Settings → Platforms is the canonical reconnect surface.
                action: { label: 'Open settings', href: '#/settings' },
            };
        }
        return null;
    }

    function renderPageBanner(code, entry) {
        const headerEl = document.querySelector('#app .page-header');
        if (!headerEl) return;
        const existing = document.querySelector('#app .platform-status-banner');
        const content = bannerContent(code, entry);
        if (!content) {
            if (existing) existing.remove();
            return;
        }
        let banner = existing;
        if (!banner) {
            banner = document.createElement('div');
            banner.className = 'platform-status-banner';
            headerEl.insertAdjacentElement('afterend', banner);
        }
        banner.className = `platform-status-banner status-banner-${content.kind}`;
        const actionHTML = content.action
            ? `<a class="banner-action" href="${content.action.href}">${content.action.label}</a>`
            : '';
        // textContent escaping via DOM API for the dynamic strings;
        // static markup can use innerHTML safely.
        banner.innerHTML = `
            <div class="banner-icon" aria-hidden="true">${content.kind === 'throttled' ? '⏳' : '⚠'}</div>
            <div class="banner-text">
                <div class="banner-title"></div>
                <div class="banner-body"></div>
            </div>
            ${actionHTML}
        `;
        banner.querySelector('.banner-title').textContent = content.title;
        banner.querySelector('.banner-body').textContent = content.body;
    }

    /* App-wide banner for CONFIRMED-dead sessions (session.status ===
     * 'expired', folded into /platforms/health by the periodic session check).
     * Unlike renderPageBanner (which is scoped to the current platform's own
     * dashboard header), this is pinned at the top of #main-col so it shows on
     * EVERY page until the session is valid again — the "you must fix this"
     * layer. 'error' (uncertain / network blip) is deliberately NOT bannered
     * here to avoid false alarms; it still shows amber on the Settings dots. */
    function renderGlobalBanner() {
        const host = document.getElementById('main-col');
        if (!host) return;
        const codes = PLATFORMS.filter((code) => _data[code] && _data[code].session
            && _data[code].session.status === 'expired');
        const labels = codes.map((code) => LABELS[code] || code.toUpperCase());
        let banner = document.getElementById('pp-global-banner');
        if (!codes.length) {
            if (banner) banner.remove();
            return;
        }
        if (!banner) {
            banner = document.createElement('div');
            banner.id = 'pp-global-banner';
            banner.className = 'pp-global-banner';
            host.insertBefore(banner, host.firstChild);
        }
        const title = codes.length === 1
            ? `${labels[0]} session expired`
            : `${codes.length} platform sessions expired`;
        const body = codes.length === 1
            ? 'Re-enter your credentials — polling and posting to it will keep failing until you do.'
            : labels.join(', ');
        // A single expired platform we can quick-reconnect → a Reconnect button
        // that opens the paste-a-token modal; otherwise link to Settings (also
        // the multi-platform case).
        const one = codes.length === 1 ? codes[0] : null;
        const canRc = one && window.Reconnect && Reconnect.canReconnect(one);
        const action = canRc
            ? `<button class="pp-gb-action" type="button" data-reconnect="${one}">Reconnect →</button>`
            : `<a class="pp-gb-action" href="#/settings/platforms">Fix in Settings →</a>`;
        banner.innerHTML = `
            <span class="pp-gb-icon" aria-hidden="true">⚠</span>
            <span class="pp-gb-text"><strong class="pp-gb-title"></strong> <span class="pp-gb-body"></span></span>
            ${action}
        `;
        banner.querySelector('.pp-gb-title').textContent = title;
        banner.querySelector('.pp-gb-body').textContent = body;
        const rcBtn = banner.querySelector('[data-reconnect]');
        if (rcBtn) rcBtn.addEventListener('click', () => Reconnect.open(rcBtn.dataset.reconnect));
    }

    function notify() {
        _listeners.forEach((fn) => {
            try { fn(_data); } catch (e) { console.error('[platform_health] listener', e); }
        });
    }

    async function fetchOnce() {
        try {
            const resp = await fetch('/api/platforms/health');
            if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
            _data = await resp.json();
            renderDots();
            renderPageSubtitle();
            renderGlobalBanner();
            notify();
        } catch (e) {
            // Auth blips and partial deploys shouldn't spam the
            // console; the tick will retry in 60s.
            console.debug('[platform_health] fetch failed', e);
        }
    }

    function start() {
        if (_timer) return;
        fetchOnce();
        _timer = setInterval(fetchOnce, POLL_INTERVAL_MS);
        // Lighter 30s tick to keep the relative-time display
        // ("3m ago" → "4m ago") fresh between full health refreshes.
        // No HTTP — just re-renders from cached data.
        setInterval(() => { renderDots(); renderPageSubtitle(); renderGlobalBanner(); }, 30_000);
        // Re-inject subtitle whenever the SPA navigates between
        // platform dashboards. The render methods fetch data
        // asynchronously before writing the page-header into #app,
        // so a fixed-delay setTimeout is unreliable. Use a
        // MutationObserver to fire whenever #app's subtree changes
        // and re-render the subtitle, throttled to one rAF tick
        // so input typing / chart redraws / hover state changes
        // don't trigger a renderPageSubtitle flood.
        const appEl = document.getElementById('app');
        if (appEl && 'MutationObserver' in window) {
            let scheduled = false;
            const observer = new MutationObserver(() => {
                if (scheduled) return;
                scheduled = true;
                requestAnimationFrame(() => {
                    scheduled = false;
                    renderPageSubtitle();
                });
            });
            observer.observe(appEl, { childList: true, subtree: true });
        }
    }

    function stop() {
        if (_timer) clearInterval(_timer);
        _timer = null;
    }

    window.PlatformHealth = {
        start, stop, fetchOnce,
        get: (code) => _data[code] || null,
        getAll: () => _data,
        classify: (code) => classify(_data[code]),
        relativePast, relativeFuture,
        subscribe: (fn) => { _listeners.add(fn); return () => _listeners.delete(fn); },
        LABELS,
    };

    // Auto-start once the dashboard auth check passes. We can't
    // unconditionally start at DOM ready because /api/platforms/health
    // requires a valid session; the login page would then 401-spam
    // every minute. Instead we wait for app.js to flip the page out
    // of the login state. App.init() calls PlatformHealth.start()
    // explicitly after dashboard-status confirms authenticated.
    // The fallback below is for legacy pages that bypass App.init.
    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', () => {
            // Only auto-start if we've already loaded past login —
            // detect via the presence of an auth cookie or the
            // platform-grid being present in the DOM.
            if (document.getElementById('platform-grid')) start();
        });
    } else if (document.getElementById('platform-grid')) {
        start();
    }
})();
