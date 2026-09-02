/* PawPoller floating logs panel — opt-in widget that tails
 * logs/{server,app,polling}.log via SSE.
 *
 * Bottom-right toggle ("Logs") above the toast stack. Click to
 * expand a 460x340 panel with a level filter, file picker, pause
 * toggle, and clear button. The EventSource opens only when the
 * panel is open — no idle connection when collapsed.
 *
 * Persists open/collapsed state, selected file, and panel position
 * in localStorage so the user's setup survives across navigations.
 *
 * Auto-scroll sticks to bottom unless the user has scrolled up,
 * matching the convention of every terminal log viewer the user
 * has used before.
 *
 * Gated by the `logs_panel_enabled` preference (Settings → App
 * Preferences → "Floating logs button", default on). The button is
 * only rendered when enabled; the settings toggle flips it live via
 * window.LogsPanel.setEnabled without a reload.
 */

(function () {
    const STORAGE_KEY = 'pp_logs_panel_state';
    const FILES = ['app', 'server', 'polling'];
    const LEVELS = ['all', 'debug', 'info', 'warning', 'error'];
    const MAX_LINES = 1500;

    let state = loadState();
    let enabled = true;   // gated by the logs_panel_enabled preference (default on)
    let toggleEl = null;
    let panelEl = null;
    let bodyEl = null;
    let evtSource = null;
    let stickToBottom = true;
    let paused = false;
    let lineCount = 0;

    function loadState() {
        try {
            const raw = localStorage.getItem(STORAGE_KEY);
            const parsed = raw ? JSON.parse(raw) : {};
            return {
                open: !!parsed.open,
                file: FILES.includes(parsed.file) ? parsed.file : 'app',
                level: LEVELS.includes(parsed.level) ? parsed.level : 'all',
            };
        } catch (e) {
            return { open: false, file: 'app', level: 'all' };
        }
    }

    function saveState() {
        try {
            localStorage.setItem(STORAGE_KEY, JSON.stringify(state));
        } catch (e) { /* quota / private mode — drop quietly */ }
    }

    function ensureToggle() {
        if (toggleEl) return;
        toggleEl = document.createElement('button');
        toggleEl.className = 'pp-logs-toggle';
        toggleEl.type = 'button';
        toggleEl.title = 'Open logs panel (live tail)';
        toggleEl.innerHTML = '<span class="pp-logs-toggle-icon">≡</span><span class="pp-logs-toggle-label">Logs</span>';
        toggleEl.addEventListener('click', toggle);
        document.body.appendChild(toggleEl);
    }

    function ensurePanel() {
        if (panelEl) return;
        panelEl = document.createElement('div');
        panelEl.className = 'pp-logs-panel';
        panelEl.setAttribute('hidden', '');
        panelEl.innerHTML = `
            <div class="pp-logs-header" data-pp-logs-drag>
                <select class="pp-logs-file" title="Log file">
                    ${FILES.map(f => `<option value="${f}">${f}.log</option>`).join('')}
                </select>
                <select class="pp-logs-level" title="Filter by level">
                    ${LEVELS.map(l => `<option value="${l}">${l === 'all' ? 'all levels' : l}</option>`).join('')}
                </select>
                <button class="pp-logs-pause" type="button" title="Pause / resume">⏸</button>
                <button class="pp-logs-clear" type="button" title="Clear view (server log untouched)">✕ clear</button>
                <button class="pp-logs-close" type="button" title="Close panel">×</button>
            </div>
            <div class="pp-logs-body" tabindex="0"></div>
            <div class="pp-logs-footer">
                <span class="pp-logs-status">Connecting…</span>
                <span class="pp-logs-count">0 lines</span>
            </div>
        `;
        document.body.appendChild(panelEl);

        bodyEl = panelEl.querySelector('.pp-logs-body');

        const fileSel = panelEl.querySelector('.pp-logs-file');
        fileSel.value = state.file;
        fileSel.addEventListener('change', () => {
            state.file = fileSel.value;
            saveState();
            clearView();
            reconnect();
        });

        const levelSel = panelEl.querySelector('.pp-logs-level');
        levelSel.value = state.level;
        levelSel.addEventListener('change', () => {
            state.level = levelSel.value;
            saveState();
            applyLevelFilter();
        });

        panelEl.querySelector('.pp-logs-pause').addEventListener('click', (e) => {
            paused = !paused;
            e.currentTarget.textContent = paused ? '▶' : '⏸';
            e.currentTarget.title = paused ? 'Resume' : 'Pause';
            updateStatus();
        });

        panelEl.querySelector('.pp-logs-clear').addEventListener('click', () => clearView());
        panelEl.querySelector('.pp-logs-close').addEventListener('click', () => toggle());

        // Auto-scroll toggle: sticks to bottom unless the user
        // scrolls up; resumes sticking when they scroll back down.
        bodyEl.addEventListener('scroll', () => {
            const slop = 24;
            stickToBottom = (bodyEl.scrollHeight - bodyEl.scrollTop - bodyEl.clientHeight) < slop;
        });
    }

    function clearView() {
        if (!bodyEl) return;
        bodyEl.innerHTML = '';
        lineCount = 0;
        updateCount();
    }

    function classifyLevel(line) {
        const upper = line.toUpperCase();
        if (upper.includes(' ERROR') || upper.includes(' CRITICAL')) return 'error';
        if (upper.includes(' WARN')) return 'warning';
        if (upper.includes(' DEBUG')) return 'debug';
        return 'info';
    }

    function shouldShow(level) {
        if (state.level === 'all') return true;
        if (state.level === 'debug') return true; // debug = show everything
        const order = { debug: 0, info: 1, warning: 2, error: 3 };
        return order[level] >= order[state.level];
    }

    function applyLevelFilter() {
        if (!bodyEl) return;
        bodyEl.querySelectorAll('.pp-log-line').forEach(el => {
            el.style.display = shouldShow(el.dataset.level) ? '' : 'none';
        });
    }

    function appendLine(text, isBackfill) {
        if (paused && !isBackfill) return;
        const level = classifyLevel(text);
        const row = document.createElement('div');
        row.className = `pp-log-line pp-log-${level}`;
        row.dataset.level = level;
        row.textContent = text;
        if (!shouldShow(level)) row.style.display = 'none';
        bodyEl.appendChild(row);
        lineCount++;
        // Cap memory: drop oldest lines beyond MAX_LINES.
        while (bodyEl.children.length > MAX_LINES) {
            bodyEl.removeChild(bodyEl.firstChild);
            lineCount--;
        }
        if (stickToBottom) {
            bodyEl.scrollTop = bodyEl.scrollHeight;
        }
        updateCount();
    }

    function updateCount() {
        const el = panelEl && panelEl.querySelector('.pp-logs-count');
        if (el) el.textContent = `${lineCount} line${lineCount === 1 ? '' : 's'}`;
    }

    function updateStatus(msg) {
        const el = panelEl && panelEl.querySelector('.pp-logs-status');
        if (!el) return;
        if (msg) {
            el.textContent = msg;
            return;
        }
        if (paused) el.textContent = 'Paused';
        else if (evtSource && evtSource.readyState === 1) el.textContent = `Live · ${state.file}.log`;
        else if (evtSource && evtSource.readyState === 0) el.textContent = 'Connecting…';
        else el.textContent = 'Disconnected';
    }

    function disconnect() {
        if (evtSource) {
            try { evtSource.close(); } catch (e) {}
            evtSource = null;
        }
        updateStatus();
    }

    function reconnect() {
        disconnect();
        const url = `/api/logs/stream?file=${encodeURIComponent(state.file)}&backfill=80`;
        evtSource = new EventSource(url);
        updateStatus();
        evtSource.onopen = () => updateStatus();
        evtSource.onmessage = (ev) => {
            try {
                const data = JSON.parse(ev.data);
                if (data.line) appendLine(data.line, !!data.backfill);
                else if (data.event === 'error') {
                    appendLine(`[stream] ${data.message || 'unknown error'}`, false);
                }
            } catch (e) {
                console.debug('[logs-panel] parse failed', e, ev.data);
            }
        };
        evtSource.onerror = () => {
            updateStatus('Reconnecting…');
            // EventSource auto-reconnects; nothing to do here beyond
            // surfacing the state. Don't tear down the connection or
            // we'd lose the auto-retry behaviour.
        };
    }

    function toggle() {
        ensurePanel();
        state.open = !state.open;
        saveState();
        if (state.open) {
            panelEl.removeAttribute('hidden');
            stickToBottom = true;
            reconnect();
        } else {
            panelEl.setAttribute('hidden', '');
            disconnect();
        }
    }

    // Show or hide the whole widget. When disabled, the toggle button is
    // hidden and any open panel is collapsed + disconnected. Re-enabling
    // restores the persisted open/collapsed state.
    function applyEnabled() {
        if (enabled) {
            ensureToggle();
            toggleEl.style.display = '';           // inline wins over the CSS class
            if (state.open) {
                ensurePanel();
                panelEl.removeAttribute('hidden');
                if (!evtSource) reconnect();
            }
        } else {
            if (toggleEl) toggleEl.style.display = 'none';
            if (panelEl) panelEl.setAttribute('hidden', '');
            disconnect();
        }
    }

    function setEnabled(on) {
        const next = !!on;
        if (next === enabled) { applyEnabled(); return; }
        enabled = next;
        applyEnabled();
    }

    function init() {
        // Disconnect when the tab is hidden — saves bandwidth + spares
        // the server an idle SSE connection. Reconnect on focus return.
        document.addEventListener('visibilitychange', () => {
            if (!enabled || !state.open) return;
            if (document.hidden) disconnect();
            else reconnect();
        });

        // Decide visibility from the saved preference. Default on, and on any
        // failure (offline / API not ready) keep the widget so behaviour never
        // silently regresses. Defer the open-panel restore so the page renders.
        const decide = (prefs) => {
            enabled = !(prefs && prefs.logs_panel_enabled === false);
            if (!enabled) { applyEnabled(); return; }
            ensureToggle();
            if (state.open) setTimeout(applyEnabled, 200);
        };
        if (typeof API !== 'undefined' && API.getPreferences) {
            API.getPreferences().then(decide).catch(() => decide(null));
        } else {
            decide(null);
        }
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', init);
    } else {
        init();
    }

    window.LogsPanel = {
        open: () => { if (enabled && !state.open) toggle(); },
        close: () => { if (state.open) toggle(); },
        toggle: () => { if (enabled) toggle(); },
        setEnabled,
    };
})();
