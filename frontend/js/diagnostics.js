/*
 * Diagnostics tab module.
 *
 * Two-pane layout:
 *   - Left: categorized test catalog with per-test run buttons +
 *     last-known status
 *   - Right: live log stream + summary card + run controls
 *
 * SSE stream from /api/testing/stream/{run_id} feeds the log panel
 * and updates the catalog rows as tests progress. Run history +
 * last status persisted server-side at data/diagnostics_results.json.
 */
(function () {
    'use strict';

    const STATUS_ICON = {
        passed: '✓',
        failed: '✗',
        error: '⚠',
        skipped: '−',
        running: '▶',
        pending: '○',
    };
    const STATUS_CLASS = {
        passed: 'diag-status-passed',
        failed: 'diag-status-failed',
        error: 'diag-status-error',
        skipped: 'diag-status-skipped',
        running: 'diag-status-running',
        pending: 'diag-status-pending',
    };
    const LOG_LEVEL_CLASS = {
        error: 'diag-log-error',
        warn: 'diag-log-warn',
        info: 'diag-log-info',
        debug: 'diag-log-debug',
    };
    const LOG_MAX_LINES = 5000;

    const Diagnostics = {
        _mounted: false,
        _tests: [],                  // [{test_id, name, category, ...}]
        _byId: new Map(),            // test_id → spec
        _results: new Map(),         // test_id → last result {status, duration_ms, message, details}
        _categories: [],
        _summary: null,              // last persisted summary
        _activeRunId: null,
        _eventSource: null,
        _logBuffer: [],              // [{level, message, ts, test_id}]
        _logEl: null,
        _summaryEl: null,
        _autoScroll: true,
        _logFilter: 'all',
        _searchText: '',
        _runProgress: null,          // {total, done, passed, failed, errored, skipped}

        async mount(root) {
            if (!root) return;
            try {
                const tests = await fetch('/api/testing/tests', { credentials: 'same-origin' }).then(r => r.json());
                this._tests = tests.tests || [];
                this._categories = tests.categories || [];
                this._summary = tests.summary || null;
                this._byId.clear();
                this._results.clear();
                for (const t of this._tests) {
                    this._byId.set(t.test_id, t);
                    if (t.last_status) {
                        this._results.set(t.test_id, {
                            status: t.last_status,
                            duration_ms: t.last_duration_ms,
                            message: t.last_message,
                        });
                    }
                }
                this._render(root);
                this._mounted = true;
                // Re-attach to an in-flight run if one exists
                const act = await fetch('/api/testing/active', { credentials: 'same-origin' }).then(r => r.json());
                if (act && act.run_id) {
                    this._attachStream(act.run_id);
                }
            } catch (err) {
                root.innerHTML = `<div class="diag-error">Failed to load diagnostics: ${this._esc(err.message || err)}</div>`;
            }
        },

        _render(root) {
            const catalog = this._categories.map(cat => this._renderCategory(cat)).join('');
            root.innerHTML = `
                <div class="diag-wrap">
                    <div class="diag-toolbar">
                        <input type="search" class="diag-search" placeholder="Filter tests…" />
                        <select class="diag-status-filter">
                            <option value="all">All statuses</option>
                            <option value="passed">Passed</option>
                            <option value="failed">Failed</option>
                            <option value="error">Errored</option>
                            <option value="skipped">Skipped</option>
                            <option value="pending">Not yet run</option>
                        </select>
                        <span class="diag-toolbar-spacer"></span>
                        ${this._renderSummaryBadge()}
                    </div>
                    <div class="diag-grid">
                        <div class="diag-catalog">${catalog}</div>
                        <div class="diag-stream">
                            ${this._renderActions()}
                            <div class="diag-summary" id="diag-summary">${this._renderSummaryCard()}</div>
                            <div class="diag-log-toolbar">
                                <span class="diag-log-title">Live log</span>
                                <span class="diag-log-spacer"></span>
                                <select class="diag-log-filter">
                                    <option value="all">All</option>
                                    <option value="error">Errors</option>
                                    <option value="warn">Warnings</option>
                                    <option value="info">Info</option>
                                </select>
                                <label class="diag-checkbox">
                                    <input type="checkbox" id="diag-autoscroll" checked /> auto-scroll
                                </label>
                                <button class="btn btn-xs" id="diag-log-clear">Clear</button>
                                <button class="btn btn-xs" id="diag-log-download">Download</button>
                            </div>
                            <pre class="diag-log" id="diag-log"></pre>
                        </div>
                    </div>
                </div>
            `;
            this._logEl = root.querySelector('#diag-log');
            this._summaryEl = root.querySelector('#diag-summary');
            this._bindEvents(root);
        },

        _renderCategory(cat) {
            const tests = this._tests.filter(t => t.category === cat);
            const counts = {passed: 0, failed: 0, error: 0, skipped: 0, pending: 0};
            for (const t of tests) {
                counts[t.last_status || 'pending'] = (counts[t.last_status || 'pending'] || 0) + 1;
            }
            const summary = [];
            if (counts.passed) summary.push(`<span class="diag-status-passed">✓ ${counts.passed}</span>`);
            if (counts.failed) summary.push(`<span class="diag-status-failed">✗ ${counts.failed}</span>`);
            if (counts.error)  summary.push(`<span class="diag-status-error">⚠ ${counts.error}</span>`);
            if (counts.skipped) summary.push(`<span class="diag-status-skipped">− ${counts.skipped}</span>`);
            if (counts.pending) summary.push(`<span class="diag-status-pending">○ ${counts.pending}</span>`);
            return `
                <details class="diag-category" open data-category="${this._esc(cat)}">
                    <summary class="diag-category-header">
                        <span class="diag-category-name">${this._esc(cat)}</span>
                        <span class="diag-category-counts">${summary.join(' ')}</span>
                        <button class="btn btn-xs diag-run-category" data-category="${this._esc(cat)}" title="Run all tests in this category">Run category</button>
                    </summary>
                    <div class="diag-category-body">
                        ${tests.map(t => this._renderTestRow(t)).join('')}
                    </div>
                </details>
            `;
        },

        _renderTestRow(t) {
            const result = this._results.get(t.test_id) || {};
            const status = result.status || 'pending';
            const icon = STATUS_ICON[status] || '○';
            const cls = STATUS_CLASS[status] || 'diag-status-pending';
            const duration = result.duration_ms != null
                ? `<span class="diag-test-duration">${Math.round(result.duration_ms)} ms</span>`
                : '';
            const msg = result.message
                ? `<span class="diag-test-message">${this._esc(result.message)}</span>`
                : '';
            const destBadge = t.destructive
                ? '<span class="diag-test-destructive" title="Destructive — explicit confirmation required">⚠ destructive</span>'
                : '';
            return `
                <div class="diag-test" data-test-id="${this._esc(t.test_id)}" data-status="${status}">
                    <span class="diag-test-icon ${cls}">${icon}</span>
                    <span class="diag-test-name" title="${this._esc(t.description || '')}">${this._esc(t.name)}</span>
                    ${destBadge}
                    ${duration}
                    ${msg}
                    <button class="btn btn-xs diag-run-test" data-test-id="${this._esc(t.test_id)}" data-destructive="${t.destructive ? '1' : '0'}">Run</button>
                </div>
            `;
        },

        _renderActions() {
            return `
                <div class="diag-actions">
                    <button class="btn btn-success diag-run-all" id="diag-run-all" title="Run everything (destructive tests skipped)">▶ Run All</button>
                    <button class="btn btn-secondary diag-run-failed" id="diag-run-failed" title="Re-run only tests that failed last run">Re-run failed</button>
                    <button class="btn btn-secondary diag-stop" id="diag-stop" disabled>Stop</button>
                    <span class="diag-actions-spacer"></span>
                    <label class="diag-checkbox" title="Include destructive tests in run-all (you'll confirm each one)">
                        <input type="checkbox" id="diag-include-destructive" /> include destructive
                    </label>
                </div>
            `;
        },

        _renderSummaryBadge() {
            if (!this._summary) {
                return '<span class="diag-summary-badge">never run</span>';
            }
            const s = this._summary;
            const parts = [];
            if (s.passed) parts.push(`<span class="diag-status-passed">${s.passed} ✓</span>`);
            if (s.failed) parts.push(`<span class="diag-status-failed">${s.failed} ✗</span>`);
            if (s.errored) parts.push(`<span class="diag-status-error">${s.errored} ⚠</span>`);
            if (s.skipped) parts.push(`<span class="diag-status-skipped">${s.skipped} −</span>`);
            return `<span class="diag-summary-badge">last run: ${parts.join(' ')}</span>`;
        },

        _renderSummaryCard() {
            const p = this._runProgress;
            const s = this._summary;
            if (p) {
                const total = p.total || 0;
                const done = p.done || 0;
                const pct = total ? Math.round((done / total) * 100) : 0;
                return `
                    <div class="diag-summary-card running">
                        <div class="diag-summary-row">
                            <strong>Running:</strong> ${done} / ${total} (${pct}%)
                        </div>
                        <div class="diag-summary-row">
                            <span class="diag-status-passed">${p.passed || 0} ✓</span>
                            <span class="diag-status-failed">${p.failed || 0} ✗</span>
                            <span class="diag-status-error">${p.errored || 0} ⚠</span>
                            <span class="diag-status-skipped">${p.skipped || 0} −</span>
                        </div>
                        <div class="diag-progress-bar">
                            <div class="diag-progress-fill" style="width:${pct}%"></div>
                        </div>
                    </div>
                `;
            }
            if (!s) {
                return '<div class="diag-summary-card empty">No suite run yet. Click ▶ Run All to start.</div>';
            }
            const total = s.total || 0;
            const ok = s.passed || 0;
            return `
                <div class="diag-summary-card ${(s.failed || s.errored) ? 'has-failures' : 'all-green'}">
                    <div class="diag-summary-row">
                        <strong>Last run:</strong> ${ok} / ${total} passed
                    </div>
                    <div class="diag-summary-row">
                        <span class="diag-status-passed">${s.passed || 0} ✓</span>
                        <span class="diag-status-failed">${s.failed || 0} ✗</span>
                        <span class="diag-status-error">${s.errored || 0} ⚠</span>
                        <span class="diag-status-skipped">${s.skipped || 0} −</span>
                        <span class="diag-summary-duration">${((s.duration_ms || 0) / 1000).toFixed(1)}s</span>
                    </div>
                </div>
            `;
        },

        _bindEvents(root) {
            // Run single test
            root.addEventListener('click', async (e) => {
                const runBtn = e.target.closest('.diag-run-test');
                if (runBtn) {
                    const tid = runBtn.dataset.testId;
                    const dest = runBtn.dataset.destructive === '1';
                    await this._runOne(tid, dest);
                    return;
                }
                const catBtn = e.target.closest('.diag-run-category');
                if (catBtn) {
                    await this._runCategory(catBtn.dataset.category);
                    return;
                }
                if (e.target.id === 'diag-run-all') {
                    await this._runSuite(false);
                    return;
                }
                if (e.target.id === 'diag-run-failed') {
                    await this._runSuite(true);
                    return;
                }
                if (e.target.id === 'diag-stop') {
                    if (this._activeRunId) {
                        await fetch(`/api/testing/stop/${this._activeRunId}`, {
                            method: 'POST',
                            credentials: 'same-origin',
                        });
                        this._appendLog({level: 'warn', message: 'Stop requested — finishing current test, then halting.', timestamp: Date.now() / 1000});
                    }
                    return;
                }
                if (e.target.id === 'diag-log-clear') {
                    this._logBuffer = [];
                    if (this._logEl) this._logEl.innerHTML = '';
                    return;
                }
                if (e.target.id === 'diag-log-download') {
                    this._downloadLog();
                    return;
                }
            });

            const searchEl = root.querySelector('.diag-search');
            if (searchEl) searchEl.addEventListener('input', () => {
                this._searchText = searchEl.value.toLowerCase();
                this._applyFilters(root);
            });
            const statusFilter = root.querySelector('.diag-status-filter');
            if (statusFilter) statusFilter.addEventListener('change', () => this._applyFilters(root));
            const logFilter = root.querySelector('.diag-log-filter');
            if (logFilter) logFilter.addEventListener('change', () => {
                this._logFilter = logFilter.value;
                this._redrawLog();
            });
            const autoScroll = root.querySelector('#diag-autoscroll');
            if (autoScroll) autoScroll.addEventListener('change', () => {
                this._autoScroll = autoScroll.checked;
            });
        },

        _applyFilters(root) {
            const statusFilter = root.querySelector('.diag-status-filter').value;
            const text = this._searchText;
            root.querySelectorAll('.diag-test').forEach(row => {
                const tid = row.dataset.testId;
                const t = this._byId.get(tid);
                const status = (this._results.get(tid) || {}).status || 'pending';
                let visible = true;
                if (statusFilter !== 'all' && status !== statusFilter) visible = false;
                if (text && !(t.name.toLowerCase().includes(text) || t.test_id.toLowerCase().includes(text) || t.category.toLowerCase().includes(text))) {
                    visible = false;
                }
                row.style.display = visible ? '' : 'none';
            });
        },

        async _runOne(testId, destructive) {
            if (destructive) {
                const t = this._byId.get(testId);
                const desc = t ? (t.description || t.name) : testId;
                if (!confirm(`This test is destructive:\n\n${desc}\n\nContinue?`)) return;
            }
            try {
                const resp = await fetch(`/api/testing/run/${encodeURIComponent(testId)}`, {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    credentials: 'same-origin',
                    body: JSON.stringify({confirm_destructive: destructive}),
                });
                const data = await resp.json();
                if (!resp.ok) {
                    if (resp.status === 409 && data.detail && data.detail.active_run_id) {
                        this._appendLog({level: 'warn', message: `Run already in progress (${data.detail.active_run_id}); attaching.`, timestamp: Date.now() / 1000});
                        this._attachStream(data.detail.active_run_id);
                        return;
                    }
                    alert(`Run failed: ${JSON.stringify(data.detail || data)}`);
                    return;
                }
                this._attachStream(data.run_id);
            } catch (err) {
                alert(`Network error: ${err.message || err}`);
            }
        },

        async _runCategory(category) {
            // Find destructive tests in this category. If any exist, ask up
            // front whether to include them; otherwise skip.
            const destructiveTests = this._tests.filter(t => t.category === category && t.destructive);
            let opt_in = [];
            if (destructiveTests.length) {
                const names = destructiveTests.map(t => '• ' + t.name).join('\n');
                if (confirm(`This category includes destructive tests:\n${names}\n\nInclude them?`)) {
                    opt_in = destructiveTests.map(t => t.test_id);
                }
            }
            try {
                const resp = await fetch(`/api/testing/run-category/${encodeURIComponent(category)}`, {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    credentials: 'same-origin',
                    body: JSON.stringify({include_destructive: opt_in}),
                });
                const data = await resp.json();
                if (!resp.ok) {
                    if (resp.status === 409 && data.detail && data.detail.active_run_id) {
                        this._attachStream(data.detail.active_run_id);
                        return;
                    }
                    alert(`Run failed: ${JSON.stringify(data.detail || data)}`);
                    return;
                }
                this._attachStream(data.run_id);
            } catch (err) {
                alert(`Network error: ${err.message || err}`);
            }
        },

        async _runSuite(onlyFailed) {
            const inc = document.getElementById('diag-include-destructive');
            let opt_in = [];
            if (inc && inc.checked) {
                const destructives = this._tests.filter(t => t.destructive);
                const names = destructives.map(t => '• ' + t.name).join('\n');
                if (!confirm(`Destructive tests will run:\n${names}\n\nContinue?`)) return;
                opt_in = destructives.map(t => t.test_id);
            }
            try {
                const resp = await fetch('/api/testing/run-suite', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    credentials: 'same-origin',
                    body: JSON.stringify({include_destructive: opt_in, only_failed: onlyFailed}),
                });
                const data = await resp.json();
                if (!resp.ok) {
                    if (resp.status === 409 && data.detail && data.detail.active_run_id) {
                        this._attachStream(data.detail.active_run_id);
                        return;
                    }
                    alert(`Run failed: ${JSON.stringify(data.detail || data)}`);
                    return;
                }
                this._attachStream(data.run_id);
            } catch (err) {
                alert(`Network error: ${err.message || err}`);
            }
        },

        _attachStream(runId) {
            if (this._eventSource) {
                try { this._eventSource.close(); } catch (e) { /* noop */ }
            }
            this._activeRunId = runId;
            this._runProgress = {total: 0, done: 0, passed: 0, failed: 0, errored: 0, skipped: 0};
            const stopBtn = document.getElementById('diag-stop');
            if (stopBtn) stopBtn.disabled = false;
            this._updateSummaryCard();

            const es = new EventSource(`/api/testing/stream/${encodeURIComponent(runId)}`);
            this._eventSource = es;
            es.onmessage = (e) => {
                try {
                    const ev = JSON.parse(e.data);
                    this._handleEvent(ev);
                } catch (err) {
                    /* ignore malformed event */
                }
            };
            es.onerror = () => {
                /* connection might end naturally; cleanup happens on eof event */
            };
        },

        _handleEvent(ev) {
            switch (ev.event) {
                case 'suite_start':
                    this._runProgress.total = ev.total || 0;
                    this._appendLog({level: 'info', message: `Suite starting (${ev.total} tests)`, timestamp: ev.ts});
                    this._updateSummaryCard();
                    break;
                case 'test_start':
                    this._setTestStatus(ev.test_id, 'running', null);
                    this._appendLog({level: 'info', message: `▶ ${ev.name} [${ev.category}]`, timestamp: ev.ts, test_id: ev.test_id});
                    break;
                case 'log':
                    this._appendLog({level: ev.level || 'info', message: ev.message, timestamp: ev.ts, test_id: ev.test_id});
                    break;
                case 'test_end': {
                    this._setTestStatus(ev.test_id, ev.status, ev.duration_ms, ev.message);
                    const icon = STATUS_ICON[ev.status] || '?';
                    const dur = ev.duration_ms != null ? ` (${Math.round(ev.duration_ms)} ms)` : '';
                    const msg = ev.message ? ` — ${ev.message}` : '';
                    this._appendLog({
                        level: ev.status === 'passed' ? 'info' : (ev.status === 'skipped' ? 'warn' : 'error'),
                        message: `${icon} ${ev.test_id}${dur}${msg}`,
                        timestamp: ev.ts,
                        test_id: ev.test_id,
                    });
                    this._runProgress.done = (this._runProgress.done || 0) + 1;
                    if (ev.status === 'passed') this._runProgress.passed++;
                    else if (ev.status === 'failed') this._runProgress.failed++;
                    else if (ev.status === 'error') this._runProgress.errored++;
                    else if (ev.status === 'skipped') this._runProgress.skipped++;
                    this._updateSummaryCard();
                    break;
                }
                case 'suite_complete':
                    this._summary = ev.summary;
                    this._runProgress = null;
                    this._activeRunId = null;
                    if (this._eventSource) { try { this._eventSource.close(); } catch (e) { /* noop */ } }
                    this._eventSource = null;
                    const stopBtn = document.getElementById('diag-stop');
                    if (stopBtn) stopBtn.disabled = true;
                    this._appendLog({level: 'info', message: `■ Suite complete: ${ev.summary.passed} passed, ${ev.summary.failed} failed, ${ev.summary.errored} errored, ${ev.summary.skipped} skipped`, timestamp: ev.ts});
                    this._updateSummaryCard();
                    this._updateSummaryBadge();
                    break;
                case 'eof':
                    if (this._eventSource) { try { this._eventSource.close(); } catch (e) { /* noop */ } }
                    this._eventSource = null;
                    break;
                case 'runner_error':
                    this._appendLog({level: 'error', message: `Runner crashed: ${ev.message}`, timestamp: ev.ts});
                    break;
            }
        },

        _setTestStatus(testId, status, durationMs, message) {
            this._results.set(testId, {
                status,
                duration_ms: durationMs,
                message: message || '',
            });
            // Update row in place
            const row = document.querySelector(`.diag-test[data-test-id="${CSS.escape(testId)}"]`);
            if (!row) return;
            row.dataset.status = status;
            const iconEl = row.querySelector('.diag-test-icon');
            if (iconEl) {
                iconEl.className = `diag-test-icon ${STATUS_CLASS[status] || 'diag-status-pending'}`;
                iconEl.textContent = STATUS_ICON[status] || '?';
            }
            const durEl = row.querySelector('.diag-test-duration');
            if (durationMs != null) {
                if (durEl) durEl.textContent = `${Math.round(durationMs)} ms`;
                else {
                    const span = document.createElement('span');
                    span.className = 'diag-test-duration';
                    span.textContent = `${Math.round(durationMs)} ms`;
                    row.querySelector('.diag-run-test').before(span);
                }
            }
            const msgEl = row.querySelector('.diag-test-message');
            if (message) {
                if (msgEl) msgEl.textContent = message;
                else {
                    const span = document.createElement('span');
                    span.className = 'diag-test-message';
                    span.textContent = message;
                    row.querySelector('.diag-run-test').before(span);
                }
            }
        },

        _updateSummaryCard() {
            if (this._summaryEl) this._summaryEl.innerHTML = this._renderSummaryCard();
        },

        _updateSummaryBadge() {
            const badge = document.querySelector('.diag-summary-badge');
            if (!badge || !this._summary) return;
            const s = this._summary;
            const parts = [];
            if (s.passed) parts.push(`<span class="diag-status-passed">${s.passed} ✓</span>`);
            if (s.failed) parts.push(`<span class="diag-status-failed">${s.failed} ✗</span>`);
            if (s.errored) parts.push(`<span class="diag-status-error">${s.errored} ⚠</span>`);
            if (s.skipped) parts.push(`<span class="diag-status-skipped">${s.skipped} −</span>`);
            badge.innerHTML = `last run: ${parts.join(' ')}`;
        },

        _appendLog(entry) {
            this._logBuffer.push(entry);
            if (this._logBuffer.length > LOG_MAX_LINES) {
                this._logBuffer = this._logBuffer.slice(-LOG_MAX_LINES);
            }
            this._renderLogLine(entry);
        },

        _renderLogLine(entry) {
            if (!this._logEl) return;
            if (this._logFilter !== 'all' && entry.level !== this._logFilter) return;
            const ts = new Date((entry.timestamp || Date.now() / 1000) * 1000).toISOString().substring(11, 23);
            const cls = LOG_LEVEL_CLASS[entry.level] || 'diag-log-info';
            const line = document.createElement('div');
            line.className = `diag-log-line ${cls}`;
            line.textContent = `${ts}  ${entry.message}`;
            this._logEl.appendChild(line);
            if (this._autoScroll) {
                this._logEl.scrollTop = this._logEl.scrollHeight;
            }
        },

        _redrawLog() {
            if (!this._logEl) return;
            this._logEl.innerHTML = '';
            for (const entry of this._logBuffer) this._renderLogLine(entry);
        },

        _downloadLog() {
            const lines = this._logBuffer.map(e => {
                const ts = new Date((e.timestamp || Date.now() / 1000) * 1000).toISOString();
                return `${ts}  [${e.level}]  ${e.message}`;
            });
            const blob = new Blob([lines.join('\n')], {type: 'text/plain'});
            const url = URL.createObjectURL(blob);
            const a = document.createElement('a');
            a.href = url;
            a.download = `pawpoller-diagnostics-${new Date().toISOString().replace(/[:.]/g, '-')}.log`;
            a.click();
            URL.revokeObjectURL(url);
        },

        _esc(str) {
            return String(str).replace(/[&<>"']/g, c => ({
                '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
            })[c]);
        },
    };

    window.Diagnostics = Diagnostics;
})();
