/* PublishCheck — Phase 6a: validation-only publishability matrix.
 *
 * Opens a modal that runs /api/editor/stories/{name}/publish-check and
 * renders a chapter × platform grid. Each cell shows whether the
 * combination is ready, blocked by validation errors, or already posted.
 *
 * No HTTP requests are made to external platforms — this is purely a
 * pre-flight check before the actual publish flow lands in Phase 6b.
 */

window.PublishCheck = (function () {
    const STATUS_LABELS = {
        ready: { icon: '✓', cls: 'cell-ready', label: 'Ready' },
        blocked: { icon: '✗', cls: 'cell-blocked', label: 'Blocked' },
        posted: { icon: '✓', cls: 'cell-posted', label: 'Posted' },
        posted_draft: { icon: '✎', cls: 'cell-posted-draft', label: 'Posted as draft (FA: in Scraps)' },
        posted_drifted: { icon: '↑', cls: 'cell-posted-drifted', label: 'Posted (local content changed)' },
        posted_stale: { icon: '!', cls: 'cell-posted-stale', label: 'Posted (now blocked)' },
        deleted_upstream: { icon: '⊘', cls: 'cell-deleted', label: 'Deleted on platform — re-post?' },
        ready_retry: { icon: '↻', cls: 'cell-retry', label: 'Failed prev — retry?' },
        failed_prev: { icon: '✗', cls: 'cell-blocked', label: 'Blocked + prev failed' },
        not_supported: { icon: '–', cls: 'cell-na', label: 'N/A — per-chapter only' },
        no_credentials: { icon: '🔒', cls: 'cell-no-creds', label: 'No credentials configured' },
        error: { icon: '⚠', cls: 'cell-error', label: 'Error' },
    };

    function _ensureModal() {
        let modal = document.getElementById('publish-check-modal');
        if (modal) return modal;
        modal = document.createElement('div');
        modal.id = 'publish-check-modal';
        modal.className = 'publish-check-modal';
        modal.innerHTML = `
            <div class="publish-check-backdrop"></div>
            <div class="publish-check-dialog" role="dialog" aria-label="Publish check">
                <div class="publish-check-header">
                    <div>
                        <div class="publish-check-title" id="publish-check-title">Publish Check</div>
                        <div class="publish-check-subtitle" id="publish-check-subtitle"></div>
                    </div>
                    <button class="publish-check-close" id="publish-check-close" aria-label="Close">&times;</button>
                </div>
                <div class="publish-check-body" id="publish-check-body">
                    <div class="publish-check-loading">Loading...</div>
                </div>
                <div class="publish-check-footer">
                    <span class="publish-check-legend">
                        <span class="cell-legend cell-ready">✓</span> Ready
                        <span class="cell-legend cell-posted">✓</span> Posted
                        <span class="cell-legend cell-posted-draft">✎</span> Draft
                        <span class="cell-legend cell-posted-drifted">↑</span> Drifted
                        <span class="cell-legend cell-deleted">⊘</span> Deleted
                        <span class="cell-legend cell-posted-stale">!</span> Stale
                        <span class="cell-legend cell-retry">↻</span> Retry
                        <span class="cell-legend cell-blocked">✗</span> Blocked
                        <span class="cell-legend cell-no-creds">🔒</span> No creds
                        <span class="cell-legend cell-error">⚠</span> Error
                    </span>
                    <button class="btn btn-sm btn-outline" id="publish-check-drip" title="Drip schedule: chapter 1 at a start time, then one chapter every N days">💧 Drip…</button>
                    <button class="btn btn-sm btn-outline" id="publish-check-verify" title="Probe each platform to detect deletions">Verify posted</button>
                    <button class="btn btn-sm btn-outline" id="publish-check-drafts" title="Probe each platform to detect drafts (FA: Scraps)">Check drafts</button>
                    <button class="btn btn-sm btn-outline" id="bulk-all-new" title="Post every ready cell">Publish all new</button>
                    <button class="btn btn-sm btn-outline" id="bulk-all-drifted" title="Update every drifted cell">Update drifted</button>
                    <button class="btn btn-sm" id="publish-check-recheck">Re-check</button>
                </div>
            </div>
        `;
        document.body.appendChild(modal);

        modal.querySelector('.publish-check-backdrop').addEventListener('click', () => {
            // Mobile synthetic-click guard — see metadata_editor.js for the
            // full explainer. The backdrop sits under the user's finger
            // when the modal opens, and iOS fires a synthetic click ~300ms
            // after touchend on the element under the touch point. Without
            // this gate the modal closes the instant it opens on touch.
            if (Date.now() - _openedAt < 400) return;
            close();
        });
        modal.querySelector('#publish-check-close').addEventListener('click', () => close());
        modal.querySelector('#publish-check-recheck').addEventListener('click', (e) => {
            if (!_currentStory) return;
            e.currentTarget.disabled = true;
            load(_currentStory).finally(() => { e.currentTarget.disabled = false; });
        });
        modal.querySelector('#publish-check-drip').addEventListener('click', () => {
            if (_currentStory) _toggleDripPanel();
        });
        modal.querySelector('#publish-check-verify').addEventListener('click', () => {
            if (_currentStory) verify(_currentStory);
        });
        modal.querySelector('#publish-check-drafts').addEventListener('click', () => {
            if (_currentStory) probeDrafts(_currentStory);
        });
        modal.querySelector('#bulk-all-new').addEventListener('click', () => {
            const targets = _collectTargets('all_new');
            if (targets.length) _openBulkPreflight(targets, 'all_new');
        });
        modal.querySelector('#bulk-all-drifted').addEventListener('click', () => {
            const targets = _collectTargets('all_drifted');
            if (targets.length) _openBulkPreflight(targets, 'all_drifted');
        });

        document.addEventListener('keydown', (e) => {
            if (e.key === 'Escape' && modal.classList.contains('open')) close();
        });

        return modal;
    }

    let _currentStory = null;
    let _openedAt = 0;

    // ── Live refresh while a story is being posted ───────────────────────
    // The matrix used to reload ONLY when the user did something from it: a
    // post, a bulk close, or Re-check. Everything the scheduler posted in the
    // background — which is most of a drip — left the grid open and stale,
    // with nothing on screen saying so. Reported as the matrix "not updating
    // itself" while stories are being posted.
    //
    // The server now returns `active_jobs` (pending + processing queue rows
    // for THIS story), so the poll runs only while there is something to wait
    // for and stops on its own when the count reaches zero. An idle matrix
    // makes no requests at all.
    const _POLL_MS = 6000;
    let _pollTimer = null;

    function _clearPoll() {
        if (_pollTimer) { clearTimeout(_pollTimer); _pollTimer = null; }
    }

    function _busy() {
        // A reload rebuilds #publish-check-body, which destroys the action
        // panel, the drip panel and anything typed or ticked into them. Losing
        // a half-filled publish form to a background refresh is a worse bug
        // than the one being fixed, so the poll WAITS its turn — it re-arms
        // rather than skipping, and catches up as soon as the user is done.
        if (document.getElementById('drip-panel')) return true;
        if (document.querySelector('.bulk-preflight-overlay')) return true;
        const modal = document.getElementById('publish-check-modal');
        if (!modal) return true;
        if (modal.querySelector('.publish-action-panel')) return true;
        const el = document.activeElement;
        return !!(el && modal.contains(el) &&
                  /^(INPUT|SELECT|TEXTAREA)$/.test(el.tagName));
    }

    function _schedulePoll(data) {
        _clearPoll();
        const modal = document.getElementById('publish-check-modal');
        if (!modal || !modal.classList.contains('open')) return;
        if (!data || !data.active_jobs) return;
        _pollTimer = setTimeout(function () {
            _pollTimer = null;
            const m = document.getElementById('publish-check-modal');
            if (!m || !m.classList.contains('open') || !_currentStory) return;
            if (_busy()) { _schedulePoll(data); return; }
            load(_currentStory);
        }, _POLL_MS);
    }

    async function open(storyName) {
        _currentStory = storyName;
        _openedAt = Date.now();
        const modal = _ensureModal();
        modal.classList.add('open');
        document.getElementById('publish-check-title').textContent =
            'Publish Check — ' + storyName.replace(/_/g, ' ');
        document.getElementById('publish-check-subtitle').textContent = '';
        document.getElementById('publish-check-body').innerHTML =
            '<div class="publish-check-loading">Checking ' +
            'every chapter against every platform...</div>';
        await load(storyName);
    }

    function close() {
        _clearPoll();
        const modal = document.getElementById('publish-check-modal');
        if (modal) modal.classList.remove('open');
    }

    async function verify(storyName) {
        const btn = document.getElementById('publish-check-verify');
        const original = btn ? btn.textContent : 'Verify posted';
        if (btn) { btn.disabled = true; btn.textContent = 'Probing...'; }
        try {
            const resp = await fetch(
                '/api/editor/stories/' + encodeURIComponent(storyName) + '/verify',
                { method: 'POST' },
            );
            if (!resp.ok) throw new Error('HTTP ' + resp.status);
            const data = await resp.json();
            const msg = data.deleted + ' deleted, ' + data.still_live +
                ' still live, ' + data.not_probed + ' not probed';
            if (btn) { btn.textContent = msg; setTimeout(() => {
                btn.textContent = original; btn.disabled = false;
            }, 2400); }
            // Reload matrix so deletions appear as ⊘ cells
            await load(storyName);
        } catch (e) {
            if (btn) {
                btn.textContent = 'Probe failed';
                setTimeout(() => { btn.textContent = original; btn.disabled = false; }, 2000);
            }
        }
    }

    async function probeDrafts(storyName) {
        // Probe every posted publication for draft state and overlay the
        // results onto cells in-place. No DB writes — purely an ephemeral
        // overlay so the user can see "this is sitting as a draft" at a
        // glance and click through to flip it live.
        const btn = document.getElementById('publish-check-drafts');
        const original = btn ? btn.textContent : 'Check drafts';
        if (btn) { btn.disabled = true; btn.textContent = 'Probing...'; }
        try {
            const resp = await fetch(
                '/api/editor/stories/' + encodeURIComponent(storyName) + '/probe-drafts',
                { method: 'POST' },
            );
            if (!resp.ok) throw new Error('HTTP ' + resp.status);
            const data = await resp.json();
            const msg = data.drafts + ' draft, ' + data.live +
                ' live, ' + data.not_probed + ' not probed';
            if (btn) { btn.textContent = msg; setTimeout(() => {
                btn.textContent = original; btn.disabled = false;
            }, 2400); }
            _overlayDraftResults(data.results || []);
        } catch (e) {
            if (btn) {
                btn.textContent = 'Probe failed';
                setTimeout(() => { btn.textContent = original; btn.disabled = false; }, 2000);
            }
        }
    }

    function _overlayDraftResults(results) {
        // Mutate cell DOM + the JSON blob on dataset.cell so subsequent
        // detail-panel renders see the new status. Keeps the matrix in a
        // single source of truth (the DOM) without re-fetching.
        const body = document.getElementById('publish-check-body');
        if (!body) return;
        for (const r of results) {
            const td = body.querySelector(
                'tr[data-ch-idx="' + r.chapter_index + '"] td[data-plat-id="' + r.platform + '"]'
            );
            if (!td) continue;
            const cell = JSON.parse(td.dataset.cell || '{}');
            cell.is_draft = r.is_draft === true;
            if (cell.is_draft) {
                cell.status = 'posted_draft';
                const meta = STATUS_LABELS.posted_draft;
                td.className = 'publish-check-cell ' + meta.cls;
                td.textContent = meta.icon;
                td.title = meta.label;
            }
            td.dataset.cell = JSON.stringify(cell);
        }
    }

    async function load(storyName) {
        try {
            const resp = await fetch(
                '/api/editor/stories/' + encodeURIComponent(storyName) + '/publish-check'
            );
            if (!resp.ok) {
                const text = await resp.text();
                throw new Error(text || ('HTTP ' + resp.status));
            }
            const data = await resp.json();
            _render(data);
        } catch (e) {
            document.getElementById('publish-check-body').innerHTML =
                '<div class="publish-check-error">Failed to load: ' +
                _escape(e.message) + '</div>';
        }
    }

    function _render(data) {
        const body = document.getElementById('publish-check-body');
        const sub = document.getElementById('publish-check-subtitle');

        // Stats line
        let totalCells = 0, ready = 0, posted = 0, drifted = 0, deleted = 0, blocked = 0;
        for (const row of data.matrix) {
            for (const platId of Object.keys(row.cells)) {
                const cell = row.cells[platId];
                totalCells++;
                if (cell.status === 'ready' || cell.status === 'ready_retry') ready++;
                else if (cell.status === 'posted') posted++;
                else if (cell.status === 'posted_drifted') drifted++;
                else if (cell.status === 'deleted_upstream') deleted++;
                else if (cell.status === 'blocked' || cell.status === 'posted_stale' || cell.status === 'failed_prev') blocked++;
            }
        }
        sub.innerHTML =
            '<strong>' + data.matrix.length + '</strong> row(s) × ' +
            '<strong>' + data.platforms.length + '</strong> platform(s) = ' +
            totalCells + ' combinations &nbsp;|&nbsp; ' +
            '<span class="stat-posted">' + posted + ' posted</span> &nbsp;|&nbsp; ' +
            (drifted ? '<span class="stat-drifted">' + drifted + ' drifted</span> &nbsp;|&nbsp; ' : '') +
            (deleted ? '<span class="stat-deleted">' + deleted + ' deleted</span> &nbsp;|&nbsp; ' : '') +
            '<span class="stat-ready">' + ready + ' ready</span> &nbsp;|&nbsp; ' +
            '<span class="stat-blocked">' + blocked + ' blocked</span>' +
            // Says out loud that the grid is watching itself. Without this the
            // polling is invisible, and an unattended matrix looks identical
            // whether it is live or stale — which is the complaint.
            (data.active_jobs
                ? ' &nbsp;|&nbsp; <span class="stat-inflight">↻ ' + data.active_jobs +
                  ' queued / posting — refreshing</span>'
                : '');

        _schedulePoll(data);

        // Regeneration staleness warning — MASTER.md newer than generated files
        const staleBanner = document.getElementById('publish-check-stale-banner');
        if (staleBanner) staleBanner.remove();
        if (data.regen_stale) {
            const banner = document.createElement('div');
            banner.className = 'publish-check-stale-banner';
            banner.id = 'publish-check-stale-banner';
            banner.innerHTML =
                '\u26A0 MASTER.md has been modified since the last regeneration. ' +
                '<button class="btn btn-sm" id="publish-check-regen">Regenerate now</button>';
            body.parentNode.insertBefore(banner, body);
            // Use event delegation so Retry buttons work without re-binding
            banner.addEventListener('click', async function (ev) {
                const btn = ev.target.closest('#publish-check-regen');
                if (!btn) return;
                btn.disabled = true;
                btn.textContent = 'Regenerating\u2026';
                try {
                    const resp = await fetch(
                        '/api/editor/stories/' + encodeURIComponent(data.story_name) + '/regenerate',
                        { method: 'POST', headers: { 'Content-Type': 'application/json' },
                          body: JSON.stringify({ skip_pdf: false }) }
                    );
                    if (!resp.ok) throw new Error('HTTP ' + resp.status);
                    banner.innerHTML = '\u2714 Regeneration complete. Refreshing matrix\u2026';
                    banner.classList.add('publish-check-stale-banner--success');
                    setTimeout(function () { load(data.story_name); }, 600);
                } catch (e) {
                    banner.innerHTML =
                        '\u26A0 Regeneration failed: ' + _escape(e.message) +
                        ' <button class="btn btn-sm" id="publish-check-regen">Retry</button>';
                }
            });
        }

        // Build matrix — table on desktop, expandable cards on mobile.
        // Both render the same .publish-check-cell elements with
        // data-cell + data-plat-id attributes so cell-click binding +
        // bulk target collection are unchanged.
        const isMobile = (typeof App !== 'undefined') && App.isMobileLayoutActive && App.isMobileLayoutActive();
        let html = isMobile ? _renderMobileMatrix(data) : _renderDesktopMatrix(data);

        // Detail panel placeholder. The action log used to live here as a
        // sibling, but that put it in flex-column stacking next to the
        // detail panel — when the detail panel content was tall and the
        // body's gap collapsed weirdly, the log overlapped the action
        // panel. Moved into the action panel itself (see _renderActionPanel
        // → final action-log placeholder) so flow ordering is guaranteed.
        html += '<div class="publish-check-detail" id="publish-check-detail">' +
            '<div class="publish-check-detail-empty">' +
            (isMobile ? 'Tap any platform row for details.' : 'Click any cell for details.') +
            '</div></div>';

        body.innerHTML = html;

        // Cache matrix data for bulk target collection
        _lastMatrixData = data;

        // Wire up cell clicks
        _bindCellClicks(body);

        // Wire row-end bulk buttons
        body.querySelectorAll('.bulk-row-btn').forEach(btn => {
            btn.addEventListener('click', () => {
                const chIdx = parseInt(btn.dataset.chIdx);
                const targets = _collectTargets('row', chIdx);
                if (targets.length) _openBulkPreflight(targets, 'row');
            });
        });
    }

    // Desktop matrix — chapters as rows, platforms as columns. Wide
    // table; the user scans a single row to see status across every
    // platform at a glance.
    function _renderDesktopMatrix(data) {
        let html = '<div class="publish-check-table-wrap"><table class="publish-check-table">';
        html += '<thead><tr><th class="ch-col">Chapter</th>';
        for (const p of data.platforms) {
            html += '<th class="plat-col" title="' + _escape(p.name) + '">' +
                _escape(p.name) + '</th>';
        }
        html += '<th class="bulk-col"></th>';
        html += '</tr></thead><tbody>';

        for (const row of data.matrix) {
            const isFull = row.chapter_index === 0;
            const chLabel = isFull
                ? '<strong>Full story</strong> <span class="row-hint">(' +
                  _escape(row.chapter_title) + ')</span>'
                : 'Ch ' + row.chapter_index + '. ' + _escape(row.chapter_title);
            const rowCls = isFull ? 'row-full' : 'row-chapter';
            html += '<tr class="' + rowCls + '" data-ch-idx="' + row.chapter_index +
                '" data-ch-title="' + _escape(row.chapter_title) + '">' +
                '<td class="ch-col">' + chLabel + '</td>';
            for (const p of data.platforms) {
                const cell = row.cells[p.id] || { status: 'error', errors: ['Missing'] };
                html += _renderCell(cell, p);
            }
            const rowActionable = _countActionable(row, data.platforms);
            html += '<td class="bulk-col">';
            if (rowActionable > 0) {
                html += '<button class="btn btn-xs btn-outline bulk-row-btn" ' +
                    'data-ch-idx="' + row.chapter_index + '" title="Bulk publish this row">' +
                    rowActionable + '</button>';
            }
            html += '</td></tr>';
        }
        html += '</tbody></table></div>';
        return html;
    }

    // Mobile matrix — chapters as expandable cards, each chapter
    // listing its platforms vertically with name + status label
    // inline. The 11-platform-wide table is unreadable on a 430px
    // viewport; this trades a glance-comparison across platforms for
    // tap-to-drill-in within one chapter, which matches mobile use:
    // "what's the status of <chapter X>?" rather than "compare every
    // chapter against every platform". <details> handles open/close
    // natively (no JS) and remembers state per element.
    function _renderMobileMatrix(data) {
        let html = '<div class="publish-check-mobile-list">';
        for (const row of data.matrix) {
            const isFull = row.chapter_index === 0;
            const chLabel = isFull
                ? 'Full story'
                : 'Ch ' + row.chapter_index + '. ' + _escape(row.chapter_title);
            const rowCls = isFull ? 'row-full' : 'row-chapter';
            const actionable = _countActionable(row, data.platforms);

            // Status summary in the summary bar — count by class so
            // the user sees "5✓ 1↑ 2🔒" without expanding.
            let pCounts = { posted: 0, drifted: 0, ready: 0, blocked: 0, deleted: 0, draft: 0, no_creds: 0 };
            for (const p of data.platforms) {
                const c = row.cells[p.id];
                if (!c) continue;
                if (c.status === 'posted') pCounts.posted++;
                else if (c.status === 'posted_draft') pCounts.draft++;
                else if (c.status === 'posted_drifted') pCounts.drifted++;
                else if (c.status === 'ready' || c.status === 'ready_retry') pCounts.ready++;
                else if (c.status === 'deleted_upstream') pCounts.deleted++;
                else if (c.status === 'no_credentials') pCounts.no_creds++;
                else if (c.status === 'blocked' || c.status === 'posted_stale' || c.status === 'failed_prev') pCounts.blocked++;
            }
            const summaryParts = [];
            if (pCounts.posted) summaryParts.push('<span class="cell-legend cell-posted">' + pCounts.posted + '✓</span>');
            if (pCounts.draft) summaryParts.push('<span class="cell-legend cell-posted-draft">' + pCounts.draft + '✎</span>');
            if (pCounts.drifted) summaryParts.push('<span class="cell-legend cell-posted-drifted">' + pCounts.drifted + '↑</span>');
            if (pCounts.deleted) summaryParts.push('<span class="cell-legend cell-deleted">' + pCounts.deleted + '⊘</span>');
            if (pCounts.ready) summaryParts.push('<span class="cell-legend cell-ready">' + pCounts.ready + '✓</span>');
            if (pCounts.blocked) summaryParts.push('<span class="cell-legend cell-blocked">' + pCounts.blocked + '✗</span>');
            if (pCounts.no_creds) summaryParts.push('<span class="cell-legend cell-no-creds">' + pCounts.no_creds + '🔒</span>');

            html += '<details class="publish-check-mobile-row ' + rowCls + '" ' +
                'data-ch-idx="' + row.chapter_index + '" ' +
                'data-ch-title="' + _escape(row.chapter_title) + '">';
            html += '<summary class="publish-check-mobile-summary">' +
                '<span class="ch-title">' + chLabel + '</span>' +
                '<span class="ch-counts">' + summaryParts.join(' ') + '</span>' +
                '</summary>';
            html += '<div class="publish-check-mobile-platforms">';
            for (const p of data.platforms) {
                const cell = row.cells[p.id] || { status: 'error', errors: ['Missing'] };
                html += _renderMobileCell(cell, p);
            }
            if (actionable > 0) {
                html += '<button class="btn btn-sm btn-outline bulk-row-btn" ' +
                    'data-ch-idx="' + row.chapter_index + '" title="Bulk publish this chapter">' +
                    'Publish ' + actionable + ' ready' + (actionable === 1 ? '' : 's') + '</button>';
            }
            html += '</div></details>';
        }
        html += '</div>';
        return html;
    }

    function _countActionable(row, platforms) {
        let n = 0;
        for (const p of platforms) {
            const c = row.cells[p.id];
            if (c && (c.status === 'ready' || c.status === 'ready_retry' ||
                c.status === 'deleted_upstream' || c.status === 'posted_drifted')) {
                n++;
            }
        }
        return n;
    }

    // Mobile cell — same `.publish-check-cell` class so existing
    // click handler picks it up; presents as a horizontal row with
    // platform name + status label rather than a single icon cell.
    function _renderMobileCell(cell, plat) {
        const meta = STATUS_LABELS[cell.status] || STATUS_LABELS.error;
        const errSummary = (cell.errors && cell.errors.length)
            ? cell.errors[0] : '';
        return '<button type="button" class="publish-check-cell publish-check-cell-mobile ' + meta.cls + '"' +
            ' data-cell=\'' + _escape(JSON.stringify(cell)) + '\'' +
            ' data-plat-id="' + _escape(plat.id) + '"' +
            ' data-plat-name="' + _escape(plat.name) + '">' +
            '<span class="cell-icon">' + meta.icon + '</span>' +
            '<span class="cell-plat">' + _escape(plat.name) + '</span>' +
            '<span class="cell-status">' + meta.label +
            (errSummary ? ' <span class="cell-err">— ' + _escape(errSummary) + '</span>' : '') +
            '</span></button>';
    }

    function _renderCell(cell, plat) {
        const meta = STATUS_LABELS[cell.status] || STATUS_LABELS.error;
        const titleParts = [meta.label];
        if (cell.errors && cell.errors.length) {
            titleParts.push(cell.errors.join('; '));
        }
        if (cell.existing && cell.existing.external_url) {
            titleParts.push('Posted: ' + cell.existing.external_url);
        }
        return '<td class="publish-check-cell ' + meta.cls +
            '" data-cell=\'' + _escape(JSON.stringify(cell)) + '\'' +
            ' data-plat-id="' + _escape(plat.id) + '"' +
            ' data-plat-name="' + _escape(plat.name) + '"' +
            ' title="' + _escape(titleParts.join(' — ')) + '">' +
            meta.icon + '</td>';
    }

    // Cell click handler — passes plat info + chapter to detail.
    // Works for both desktop (<tr>) and mobile (<details>) wrappers
    // by walking up to whichever ancestor carries data-ch-idx.
    function _bindCellClicks(body) {
        body.querySelectorAll('.publish-check-cell').forEach(td => {
            td.addEventListener('click', () => {
                body.querySelectorAll('.publish-check-cell.selected')
                    .forEach(x => x.classList.remove('selected'));
                td.classList.add('selected');
                const wrap = td.closest('[data-ch-idx]');
                if (!wrap) return;
                const chIdx = parseInt(wrap.dataset.chIdx);
                const chTitle = wrap.dataset.chTitle;
                _showDetail(
                    JSON.parse(td.dataset.cell),
                    td.dataset.platId,
                    td.dataset.platName,
                    chIdx,
                    chTitle,
                );
                // On mobile the detail panel sits below the chapter
                // list; scroll it into view so the user sees the
                // appearing detail without hunting.
                if (typeof App !== 'undefined' && App.isMobileLayoutActive && App.isMobileLayoutActive()) {
                    requestAnimationFrame(() => {
                        document.getElementById('publish-check-detail')?.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
                    });
                }
            });
        });
    }

    function _showDetail(cell, platId, platName, chIdx, chTitle) {
        const detail = document.getElementById('publish-check-detail');
        const meta = STATUS_LABELS[cell.status] || STATUS_LABELS.error;
        let html = '<div class="publish-check-detail-header">' +
            '<span class="cell-legend ' + meta.cls + '">' + meta.icon + '</span>' +
            ' <strong>' + _escape(platName) + '</strong> &middot; ' + meta.label +
            '</div>';

        if (cell.errors && cell.errors.length) {
            html += '<div class="publish-check-detail-section"><strong>Validation errors:</strong><ul>';
            for (const err of cell.errors) {
                html += '<li>' + _escape(err) + '</li>';
            }
            html += '</ul></div>';
        }

        html += '<div class="publish-check-detail-section"><strong>Package:</strong><ul>';
        if (cell.title) html += '<li>Title: <code>' + _escape(cell.title) + '</code></li>';
        html += '<li>Tags: ' + cell.tag_count + '</li>';
        if (cell.file_path) {
            const sizeLabel = cell.file_size
                ? (cell.file_size / 1024).toFixed(0) + ' KB'
                : 'missing';
            const limit = cell.max_file_size
                ? ' / ' + (cell.max_file_size / 1024 / 1024).toFixed(0) + ' MB max'
                : '';
            html += '<li>File: <code>' + _escape(cell.file_path) + '</code> (' +
                sizeLabel + limit + ')</li>';
        }
        html += '<li>Mode required: <code>' + cell.requires_mode + '</code></li>';
        html += '<li>Supports edit: ' + (cell.supports_edit ? 'yes' : 'no — would delete+repost') + '</li>';
        html += '</ul></div>';

        if (cell.existing) {
            html += '<div class="publish-check-detail-section"><strong>Existing publication:</strong><ul>';
            html += '<li>Status: <code>' + _escape(cell.existing.status) + '</code></li>';
            // One cell can stand for more than one account's publication —
            // the grid is chapter x platform, but publications are per
            // account. The cell shows the strongest one; say so rather than
            // let the others silently disappear.
            if (cell.existing.account_count > 1) {
                html += '<li>⚠ Posted from <strong>' + cell.existing.account_count +
                    ' accounts</strong> on this platform — this cell shows account ' +
                    _escape(String(cell.existing.account_id)) +
                    '. Open the Submissions hub to see the rest.</li>';
            }
            if (cell.existing.external_id) {
                html += '<li>External ID: <code>' + _escape(cell.existing.external_id) + '</code></li>';
            }
            if (cell.existing.external_url) {
                html += '<li>URL: <a href="' + _escape(cell.existing.external_url) +
                    '" target="_blank" rel="noopener">' + _escape(cell.existing.external_url) + '</a></li>';
            }
            if (cell.existing.posted_at) {
                const rel = _relativeTime(cell.existing.posted_at);
                html += '<li>Posted: ' + _escape(cell.existing.posted_at) +
                    (rel ? ' <span class="time-relative">(' + rel + ')</span>' : '') + '</li>';
            }
            if (cell.existing.updated_at) {
                const rel = _relativeTime(cell.existing.updated_at);
                html += '<li>Last updated: ' + _escape(cell.existing.updated_at) +
                    (rel ? ' <span class="time-relative">(' + rel + ')</span>' : '') + '</li>';
            }
            html += '</ul>';

            // Manual URL anchoring + forget-publication controls — when the
            // stored URL is wrong (failed-but-actually-posted, legacy data,
            // upstream submission moved) or the user has deleted the
            // upstream submission and wants PawPoller's local memory cleared
            // so the next post is a fresh create instead of an edit.
            html += '<div class="publish-pub-controls">';
            html += '<div class="publish-pub-url-row">';
            html += '<label class="publish-pub-url-label">Set URL:</label>';
            html += '<input type="url" class="publish-pub-url-input" id="publish-pub-url-input" ' +
                'placeholder="Paste the live submission URL">';
            html += '<button class="btn btn-xs btn-outline" id="publish-pub-url-apply">Apply</button>';
            html += '</div>';
            html += '<div class="publish-pub-forget-row">';
            html += '<button class="btn btn-xs btn-outline btn-danger-text" ' +
                'id="publish-pub-forget">Forget this publication</button>';
            html += '<span class="publish-pub-forget-hint">Clears local memory only — does not touch the upstream submission.</span>';
            html += '</div>';
            html += '<div class="publish-pub-controls-result" id="publish-pub-controls-result"></div>';
            html += '</div>';

            html += '</div>';
        }

        // --- Action panel (Phase 6b) ---
        html += _renderActionPanel(cell, platId, platName, chIdx, chTitle);

        detail.innerHTML = html;
        _bindActionPanel(platId, platName, chIdx, chTitle, cell);
        // The action-log placeholder is now inside the action panel, so it's
        // recreated empty on every cell render — repopulate from the
        // session-scoped _actionLog array.
        _renderActionLog();
    }

    function _renderActionPanel(cell, platId, platName, chIdx, chTitle) {
        const isPosted = cell.existing && cell.existing.status === 'posted';
        const isDeleted = cell.status === 'deleted_upstream';
        const isDrifted = cell.status === 'posted_drifted';
        const isReady = cell.status === 'ready' || cell.status === 'ready_retry'
            || cell.status === 'posted' || cell.status === 'posted_drifted'
            || cell.status === 'deleted_upstream';
        const canEdit = cell.supports_edit;

        let html = '<div class="publish-check-detail-section publish-action-panel">';
        html += '<strong>Actions:</strong>';

        if (cell.status === 'no_credentials') {
            html += '<div class="publish-action-disabled">' +
                'No credentials configured for this platform. Set up in Settings.' +
                '</div></div>';
            return html;
        }

        if (!isReady && !isPosted) {
            html += '<div class="publish-action-disabled">' +
                'Resolve validation errors before posting.' +
                '</div></div>';
            return html;
        }

        if (isDrifted) {
            html += '<div class="publish-action-drift-banner">' +
                '<strong>Local content has changed</strong> since this was posted. ' +
                'Hit <em>Update existing</em> to push the fresh file.' +
                '</div>';
        }

        if (isDeleted) {
            html += '<div class="publish-action-deleted-banner">' +
                '<strong>Submission was deleted on ' + _escape(platName) + '.</strong> ' +
                'The previous URL is dead. Hit <em>Re-post</em> to create a new submission.' +
                '</div>';
        }

        html += '<div class="publish-action-options">';
        html += '<label><input type="checkbox" id="publish-opt-draft" checked> ' +
            'Save as draft (where supported)</label>';
        // "Post as account" selector — populated async; only shown when the
        // platform has more than one account. Absent/empty ⇒ default account.
        html += '<span id="publish-persona-row" class="persona-picker persona-picker--inline" data-persona-picker hidden></span>'
              + '<span id="publish-account-row" class="publish-account-row"></span>';
        html += '</div>';
        if (platId === 'tg') {
            // "This post only" text (4.3.0). Blank uses the story's saved
            // Telegram text, then the announcement, then a cut of the description.
            html += '<label class="pub-confirm-tgdesc publish-tg-desc">' +
                '<span>Telegram text for this post <span class="muted">— optional, this post only</span></span>' +
                '<textarea id="publish-tg-desc" rows="2" maxlength="900"></textarea></label>';
        }
        html += '<div class="publish-action-live-banner" id="publish-live-banner" style="display:none">' +
            '<strong>&#9888; LIVE PUBLISH</strong> — This will be immediately visible to ' +
            'the public on ' + _escape(platName) +
            '. Check &ldquo;Save as draft&rdquo; if you are not ready.</div>';

        html += '<div class="publish-action-buttons">';

        // Dry-run is always available
        html += '<button class="btn btn-sm btn-outline" data-publish-action="dry_run">' +
            'Dry Run (preview package)</button>';

        if (isDeleted) {
            // Re-post creates a brand new submission (goes through post, not edit)
            html += '<button class="btn btn-sm btn-primary" data-publish-action="post">' +
                'Re-post to ' + _escape(platName) + '</button>';
            html += '<button class="btn btn-sm btn-outline" data-schedule-action="post">' +
                'Schedule</button>';
        } else if (isPosted) {
            // If the draft probe flagged this as scrapped/drafted, lead with
            // "Publish draft" — the most likely reason the user is here.
            if (cell.is_draft) {
                html += '<button class="btn btn-sm btn-primary" data-publish-action="publish_draft">' +
                    'Publish draft (move out of Scraps)</button>';
            }
            const updateClass = isDrifted ? 'btn btn-sm btn-primary' : 'btn btn-sm';
            html += '<button class="' + updateClass + '" data-publish-action="update"' +
                (canEdit ? '' : ' disabled title="Platform does not support edit"') + '>' +
                'Update all' + (isDrifted ? ' (push fresh content)' : '') + '</button>';
            // Metadata-only — faster when only tags/title/summary changed
            html += '<button class="btn btn-sm btn-outline" data-publish-action="update_metadata"' +
                (canEdit ? '' : ' disabled title="Platform does not support edit"') + '>' +
                'Metadata only</button>';
            // Drift preview — shows what would actually get pushed.
            // Most useful on drifted cells but available on any
            // posted cell so the user can spot-check before any update.
            html += '<button class="btn btn-sm btn-outline" data-drift-preview="1" ' +
                'title="Show local file head + drift status before pushing">' +
                'Preview file</button>';
            if (canEdit && isDrifted) {
                html += '<button class="btn btn-sm btn-outline" data-schedule-action="update">' +
                    'Schedule update</button>';
            }
            if (cell.existing && cell.existing.external_url) {
                html += '<a class="btn btn-sm btn-outline" href="' +
                    _escape(cell.existing.external_url) +
                    '" target="_blank" rel="noopener">Open</a>';
            }
        } else if (isReady) {
            html += '<button class="btn btn-sm btn-primary" data-publish-action="post">' +
                'Post to ' + _escape(platName) + '</button>';
            html += '<button class="btn btn-sm btn-outline" data-schedule-action="post">' +
                'Schedule</button>';
        }

        html += '</div>';

        // Inline schedule form (hidden by default)
        html += '<div class="schedule-form" id="schedule-form" style="display:none">';
        html += '<div class="schedule-form-inner">';
        html += '<label class="schedule-label">Schedule for:</label>';
        html += '<input type="datetime-local" class="schedule-datetime" id="schedule-datetime">';
        html += '<div class="schedule-form-actions">';
        html += '<button class="btn btn-sm btn-primary" id="schedule-confirm">Confirm schedule</button>';
        html += '<button class="btn btn-sm btn-outline" id="schedule-cancel-form">Cancel</button>';
        html += '</div>';
        html += '</div></div>';

        // Scheduled items for this cell
        html += '<div class="schedule-pending" id="schedule-pending"></div>';

        html += '<div class="publish-action-result" id="publish-action-result"></div>';
        // Recent actions history lives at the bottom of the action panel
        // so the per-session log is guaranteed to render below the action
        // buttons, not on top of the option row (regression seen 2026-05-20
        // when the log was a sibling of the detail panel).
        html += '<div class="action-log" id="publish-action-log"></div>';
        html += '</div>';
        return html;
    }

    // Populate the "Post as" account dropdown for a platform. Only shows when
    // the platform has 2+ enabled accounts; otherwise leaves the row empty so
    // posting uses the platform's default account (unchanged behaviour). Any
    // failure degrades silently to the default account.
    async function _populateAccountSelector(platId) {
        // 4.2.0: the shared persona picker, scoped to this one platform. The
        // chips render in #publish-persona-row; the account control (label,
        // dropdown, or "no account") in #publish-account-row.
        const row = document.getElementById('publish-account-row');
        const host = document.getElementById('publish-persona-row');
        if (!row || !host || !window.Components || !Components.personaPicker) return;
        try {
            await Components.personaPicker({
                host, platforms: [platId],
                slot: () => row,
                selectClass: 'publish-account-select',
                storageKey: 'pp-persona-stories',
            });
        } catch (e) { /* default account on any failure */ }
    }

    function _bindActionPanel(platId, platName, chIdx, chTitle, cell) {
        const detail = document.getElementById('publish-check-detail');
        if (!detail) return;
        _populateAccountSelector(platId);
        detail.querySelectorAll('[data-publish-action]').forEach(btn => {
            btn.addEventListener('click', () => {
                const action = btn.dataset.publishAction;
                _executeAction(action, platId, platName, chIdx, chTitle);
            });
        });

        // Drift preview — fetches /api/posting/preview-file and
        // expands an inline panel showing the local file head +
        // hash comparison so the user can sanity-check before
        // hitting Update on a drifted cell.
        detail.querySelectorAll('[data-drift-preview]').forEach(btn => {
            btn.addEventListener('click', () => _toggleDriftPreview(btn, platId, chIdx));
        });
        const draftCb = document.getElementById('publish-opt-draft');
        const liveBanner = document.getElementById('publish-live-banner');
        if (draftCb && liveBanner) {
            draftCb.addEventListener('change', () => {
                liveBanner.style.display = draftCb.checked ? 'none' : '';
            });
        }

        // Schedule buttons — toggle inline form
        const schedForm = document.getElementById('schedule-form');
        const schedDatetime = document.getElementById('schedule-datetime');
        const schedConfirm = document.getElementById('schedule-confirm');
        const schedCancelForm = document.getElementById('schedule-cancel-form');
        let _scheduleAction = 'post';

        detail.querySelectorAll('[data-schedule-action]').forEach(btn => {
            btn.addEventListener('click', () => {
                _scheduleAction = btn.dataset.scheduleAction;
                if (schedForm) {
                    schedForm.style.display = '';
                    // Default to 1 hour from now, rounded to next 5 minutes
                    const d = new Date(Date.now() + 3600000);
                    d.setMinutes(Math.ceil(d.getMinutes() / 5) * 5, 0, 0);
                    if (schedDatetime) {
                        schedDatetime.value = _toLocalISOString(d);
                        schedDatetime.focus();
                    }
                }
            });
        });

        if (schedCancelForm) {
            schedCancelForm.addEventListener('click', () => {
                if (schedForm) schedForm.style.display = 'none';
            });
        }

        if (schedConfirm) {
            schedConfirm.addEventListener('click', () => {
                const val = schedDatetime ? schedDatetime.value : '';
                if (!val) { alert('Pick a date and time.'); return; }
                _submitSchedule(_scheduleAction, platId, platName, chIdx, chTitle, val);
            });
        }

        // Load any scheduled items for this cell
        _loadScheduledItems(platId, chIdx);

        // Bind manual URL + forget-publication controls (rendered only
        // when cell.existing — they don't exist for fresh cells).
        _bindPublicationControls(platId, platName, chIdx, chTitle, cell);
    }

    function _bindPublicationControls(platId, platName, chIdx, chTitle, cell) {
        const storyName = _currentStory;
        if (!storyName) return;

        const resultBox = document.getElementById('publish-pub-controls-result');
        const setResult = (text, cls) => {
            if (!resultBox) return;
            resultBox.innerHTML = '<div class="publish-pub-controls-msg ' +
                (cls || '') + '">' + _escape(text) + '</div>';
        };

        // --- Apply manual URL ---
        const applyBtn = document.getElementById('publish-pub-url-apply');
        const urlInput = document.getElementById('publish-pub-url-input');
        if (applyBtn && urlInput) {
            applyBtn.addEventListener('click', async () => {
                const url = (urlInput.value || '').trim();
                if (!url) {
                    setResult('Paste a URL first.', 'is-error');
                    return;
                }
                applyBtn.disabled = true;
                applyBtn.textContent = 'Saving...';
                try {
                    const resp = await fetch(
                        '/api/editor/stories/' + encodeURIComponent(storyName) +
                        '/publication',
                        {
                            method: 'PUT',
                            headers: { 'Content-Type': 'application/json' },
                            body: JSON.stringify({
                                platform: platId,
                                chapter: chIdx,
                                url: url,
                            }),
                        }
                    );
                    const data = await resp.json();
                    if (!resp.ok) {
                        throw new Error(data.detail || 'HTTP ' + resp.status);
                    }
                    setResult(
                        'URL saved (external_id=' + data.external_id + '). Refreshing...',
                        'is-success'
                    );
                    if (window.toast) {
                        window.toast.success(
                            `${platName} URL anchored (id ${data.external_id})`
                        );
                    }
                    setTimeout(() => {
                        if (_currentStory === storyName) load(storyName);
                    }, 600);
                } catch (e) {
                    setResult('Failed: ' + (e.message || e), 'is-error');
                    if (window.toast) {
                        window.toast.error(`URL save failed: ${e.message || e}`);
                    }
                } finally {
                    applyBtn.disabled = false;
                    applyBtn.textContent = 'Apply';
                }
            });
        }

        // --- Forget this publication ---
        const forgetBtn = document.getElementById('publish-pub-forget');
        if (forgetBtn) {
            forgetBtn.addEventListener('click', async () => {
                const typed = prompt(
                    'Forget PawPoller’s memory of this ' + platName +
                    ' publication for "' + chTitle + '"?\n\n' +
                    'This only clears the local row — it does NOT delete ' +
                    'anything on ' + platName + '. The cell will revert to ' +
                    '"ready" and the next post will create a fresh ' +
                    'submission rather than editing the old one.\n\n' +
                    'Type the platform code "' + platId + '" to confirm:'
                );
                if (typed === null) return; // cancelled
                if (typed !== platId) {
                    setResult('Confirmation did not match platform code.', 'is-error');
                    return;
                }
                forgetBtn.disabled = true;
                forgetBtn.textContent = 'Forgetting...';
                try {
                    const qs = '?platform=' + encodeURIComponent(platId) +
                        '&chapter=' + encodeURIComponent(chIdx) +
                        '&confirm_platform=' + encodeURIComponent(platId);
                    const resp = await fetch(
                        '/api/editor/stories/' + encodeURIComponent(storyName) +
                        '/publication' + qs,
                        { method: 'DELETE' }
                    );
                    const data = await resp.json();
                    if (!resp.ok) {
                        throw new Error(data.detail || 'HTTP ' + resp.status);
                    }
                    setResult('Publication forgotten. Refreshing...', 'is-success');
                    if (window.toast) {
                        window.toast.success(
                            `Forgot ${platName} publication for ${chTitle}`
                        );
                    }
                    setTimeout(() => {
                        if (_currentStory === storyName) load(storyName);
                    }, 600);
                } catch (e) {
                    setResult('Failed: ' + (e.message || e), 'is-error');
                    if (window.toast) {
                        window.toast.error(`Forget failed: ${e.message || e}`);
                    }
                    forgetBtn.disabled = false;
                    forgetBtn.textContent = 'Forget this publication';
                }
            });
        }
    }

    // Drift preview — toggle an inline panel showing the local
    // file head + hash comparison. Lazy-fetched on first open so a
    // drawer with the button doesn't auto-fire the read.
    async function _toggleDriftPreview(btn, platId, chIdx) {
        const storyName = _currentStory;
        if (!storyName) return;
        const detail = document.getElementById('publish-check-detail');
        if (!detail) return;
        let panel = detail.querySelector('.drift-preview-panel');
        if (panel && panel.dataset.open === '1') {
            panel.dataset.open = '0';
            panel.style.display = 'none';
            btn.textContent = 'Preview file';
            return;
        }
        if (!panel) {
            panel = document.createElement('div');
            panel.className = 'drift-preview-panel';
            btn.parentElement.insertAdjacentElement('afterend', panel);
        }
        panel.dataset.open = '1';
        panel.style.display = '';
        btn.textContent = 'Hide preview';
        panel.innerHTML = '<div class="drift-preview-loading">Loading preview…</div>';
        try {
            const params = new URLSearchParams({
                story_name: storyName,
                platform: platId,
                chapter_index: String(chIdx || 0),
            });
            const resp = await fetch('/api/posting/preview-file?' + params.toString());
            if (!resp.ok) {
                const text = await resp.text();
                throw new Error('HTTP ' + resp.status + ': ' + text.slice(0, 240));
            }
            const data = await resp.json();
            panel.innerHTML = _renderDriftPreview(data);
        } catch (e) {
            panel.innerHTML = '<div class="drift-preview-error">Preview failed: ' +
                _escape(e.message || String(e)) + '</div>';
        }
    }

    function _renderDriftPreview(data) {
        const sizeKb = (data.file_size / 1024).toFixed(1);
        const driftBadge = data.drifted
            ? '<span class="drift-badge drift-badge-drifted">drifted</span>'
            : (data.posted_hash
                ? '<span class="drift-badge drift-badge-clean">in sync</span>'
                : '<span class="drift-badge drift-badge-unposted">never posted</span>');
        const truncNote = data.excerpt_truncated
            ? '<div class="drift-preview-note">… (truncated to first ' + data.excerpt_lines + ' lines)</div>'
            : '';
        return [
            '<div class="drift-preview-header">',
            '  <div class="drift-preview-meta">',
            '    <div><strong>File:</strong> ', _escape(data.file_path), ' (', sizeKb, ' KB)</div>',
            '    <div><strong>Modified:</strong> ', _escape(data.modified_at || '—'),
            '         &nbsp;·&nbsp; <strong>Last posted:</strong> ', _escape(data.posted_at || 'never'), '</div>',
            '    <div><strong>Status:</strong> ', driftBadge, '</div>',
            '  </div>',
            '</div>',
            '<pre class="drift-preview-excerpt">', _escape(data.excerpt || ''), '</pre>',
            truncNote,
        ].join('');
    }

    async function _executeAction(action, platId, platName, chIdx, chTitle) {
        // Capture the current story NOW so we don't end up posting to or
        // reloading a different story if the user closes+reopens the modal
        // for a different story while a request is in flight.
        const storyName = _currentStory;
        if (!storyName) return;

        const draftCheckbox = document.getElementById('publish-opt-draft');
        const draft = draftCheckbox ? draftCheckbox.checked : true;
        const resultBox = document.getElementById('publish-action-result');

        // Live confirm gate
        if (action === 'post' || action === 'update' || action === 'update_metadata') {
            const draftLabel = draft ? ' as a DRAFT' : ' LIVE';
            const verb = action === 'post' ? 'Post'
                : action === 'update_metadata' ? 'Update metadata only'
                : 'Update';
            let confirmMsg = verb + ' "' + chTitle + '" to ' + platName + draftLabel + '?\n\n' +
                'This will make a real request to the platform.';
            if (!draft) {
                confirmMsg += '\n\n\u26A0 WARNING: This is a LIVE publish \u2014 it will be ' +
                    'immediately visible to the public. If you meant to save as ' +
                    'a draft, click Cancel and check the draft checkbox.';
            }
            const ok = confirm(confirmMsg);
            if (!ok) return;
        }

        if (action === 'publish_draft') {
            const ok = confirm(
                'Move "' + chTitle + '" out of Scraps on ' + platName + '?\n\n' +
                'It will become visible in the main gallery, browse, and search ' +
                'results. This is a real edit to a live submission.'
            );
            if (!ok) return;
        }

        if (resultBox) {
            resultBox.innerHTML = '<div class="publish-action-loading">Working...</div>';
        }

        try {
            const resp = await fetch(
                '/api/editor/stories/' + encodeURIComponent(storyName) + '/publish',
                {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({
                        platform: platId,
                        chapter: chIdx,
                        action: action,
                        draft: draft,
                        confirm_live: action !== 'dry_run',
                        // "Post as" account (null ⇒ platform default).
                        account_id: (function () {
                            const s = document.querySelector('.publish-account-select');
                            return s && s.value ? parseInt(s.value, 10) : null;
                        })(),
                        // Persona-first (4.2.0): the server refuses a platform this
                        // persona has no account on, rather than defaulting.
                        persona_id: (function () {
                            const h = document.getElementById('publish-persona-row');
                            return h && h.dataset.personaId ? parseInt(h.dataset.personaId, 10) : null;
                        })(),
                        description_override: ((document.getElementById('publish-tg-desc') || {}).value || '').trim() || null,
                    }),
                }
            );
            const data = await resp.json();
            _renderActionResult(data, action);
            _logAction(action, platName, chTitle, data);
            // Toast feedback for completed actions. Dry-run stays
            // silent — its result panel is the whole point.
            if (action !== 'dry_run' && window.toast) {
                if (data.ok) {
                    const verb =
                        action === 'post' ? 'Posted' :
                        action === 'update' ? 'Updated' :
                        action === 'update_metadata' ? 'Metadata updated' :
                        action === 'publish_draft' ? 'Published' :
                        'Done';
                    window.toast.success(
                        `${verb}: ${chTitle} → ${platName}`
                    );
                } else {
                    const detail = data.error || data.detail || 'unknown error';
                    window.toast.error(
                        `${platName} ${action} failed: ${detail}`
                    );
                }
            }
            if (action !== 'dry_run' && data.ok) {
                // Refresh the matrix on success — only if the user is still
                // looking at the same story. Prevents a late success callback
                // from clobbering the view if the user moved on.
                setTimeout(() => {
                    if (_currentStory === storyName) load(storyName);
                }, 800);
            }
        } catch (e) {
            if (resultBox) {
                resultBox.innerHTML = '<div class="publish-action-error">Failed: ' +
                    _escape(e.message) + '</div>';
            }
            if (window.toast) {
                window.toast.error(
                    `${platName} ${action} failed: ${e.message || e}`
                );
            }
            _logAction(action, platName, chTitle, { ok: false, error: e.message });
        }
    }

    function _renderActionResult(data, action) {
        const box = document.getElementById('publish-action-result');
        if (!box) return;

        if (action === 'dry_run') {
            const cls = data.ok ? 'publish-action-success' : 'publish-action-error';
            let html = '<div class="' + cls + '"><strong>' +
                (data.ok ? 'Dry run OK' : 'Dry run failed') + '</strong></div>';
            if (data.errors && data.errors.length) {
                html += '<ul>';
                for (const e of data.errors) html += '<li>' + _escape(e) + '</li>';
                html += '</ul>';
            }
            if (data.package) {
                const pkg = data.package;
                html += '<div class="dry-run-summary">';
                html += '<div class="dry-run-field"><span class="dry-run-label">Title</span>' +
                    _escape(pkg.title) + '</div>';
                html += '<div class="dry-run-field"><span class="dry-run-label">Rating</span>' +
                    _escape(pkg.rating) + '</div>';
                html += '<div class="dry-run-field"><span class="dry-run-label">Words</span>' +
                    (pkg.word_count || 0).toLocaleString() + '</div>';
                if (pkg.file_path) {
                    const sizeKB = pkg.file_size
                        ? (pkg.file_size / 1024).toFixed(0) + ' KB' : '\u2014';
                    html += '<div class="dry-run-field"><span class="dry-run-label">File</span>' +
                        '<code>' + _escape(pkg.file_path.split(/[/\\]/).pop()) +
                        '</code> (' + sizeKB + ')</div>';
                }
                if (pkg.tags && pkg.tags.length) {
                    html += '<div class="dry-run-field dry-run-tags-row">' +
                        '<span class="dry-run-label">Tags (' + pkg.tags.length + ')</span>' +
                        '<span class="dry-run-tags">' +
                        pkg.tags.map(t => _escape(t)).join(', ') + '</span></div>';
                }
                if (pkg.extra && Object.keys(pkg.extra).length) {
                    html += '<div class="dry-run-field"><span class="dry-run-label">Extras</span>' +
                        _escape(JSON.stringify(pkg.extra)) + '</div>';
                }
                html += '<details><summary>Raw JSON</summary><pre>' +
                    _escape(JSON.stringify(pkg, null, 2)) +
                    '</pre></details></div>';
            }
            box.innerHTML = html;
            return;
        }

        // Real post / update
        const cls = data.ok ? 'publish-action-success' : 'publish-action-error';
        let html = '<div class="' + cls + '"><strong>' +
            (data.ok ? '✓ Success' : '✗ Failed') + '</strong></div>';
        if (data.results && data.results.length) {
            for (const r of data.results) {
                html += '<div class="publish-action-result-row">';
                if (r.success) {
                    html += '<div>Posted: ';
                    if (r.external_url) {
                        html += '<a href="' + _escape(r.external_url) +
                            '" target="_blank" rel="noopener">' +
                            _escape(r.external_url) + '</a>';
                    } else if (r.external_id) {
                        html += 'ID ' + _escape(r.external_id);
                    }
                    html += '</div>';
                    if (r.duration) {
                        html += '<div class="publish-action-meta">' +
                            r.duration.toFixed(1) + 's</div>';
                    }
                } else {
                    if (r.queued_desktop) {
                        html += '<div>Queued for desktop (server unable to post). ' +
                            'Open desktop PawPoller to flush queue.</div>';
                    } else if (r.retry_queued) {
                        html += '<div>Will retry automatically with backoff.</div>';
                    }
                    if (r.error) {
                        html += '<div class="publish-action-error-msg">' +
                            _escape(r.error) + '</div>';
                    }
                }
                html += '</div>';
            }
        }
        box.innerHTML = html;
    }

    function _escape(s) {
        return String(s == null ? '' : s)
            .replace(/&/g, '&amp;')
            .replace(/</g, '&lt;')
            .replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;')
            .replace(/'/g, '&#39;');
    }

    function _relativeTime(dateStr) {
        if (!dateStr) return '';
        const d = new Date(dateStr);
        if (isNaN(d.getTime())) return '';
        const diff = Date.now() - d.getTime();
        if (diff < 0) return 'just now';
        const mins = Math.floor(diff / 60000);
        if (mins < 1) return 'just now';
        if (mins < 60) return mins + 'm ago';
        const hrs = Math.floor(mins / 60);
        if (hrs < 24) return hrs + 'h ago';
        const days = Math.floor(hrs / 24);
        if (days < 30) return days + 'd ago';
        return d.toLocaleDateString();
    }

    function _logAction(action, platName, chTitle, data) {
        _actionLog.unshift({
            time: new Date().toLocaleTimeString(),
            action: action,
            platform: platName,
            chapter: chTitle || 'Full story',
            ok: !!data.ok,
            url: data.results?.[0]?.external_url || '',
            error: data.results?.[0]?.error || data.error || '',
            queued: !!data.results?.[0]?.queued_desktop,
        });
        if (_actionLog.length > 20) _actionLog.length = 20;
        _renderActionLog();
    }

    function _renderActionLog() {
        const el = document.getElementById('publish-action-log');
        if (!el || !_actionLog.length) return;
        let html = '<div class="action-log-header">Recent actions (' +
            _actionLog.length + ')</div><div class="action-log-list">';
        for (const entry of _actionLog) {
            const icon = entry.action === 'dry_run' ? '&#128269;' :
                entry.ok ? '&#10003;' : '&#10007;';
            const cls = entry.ok ? 'log-ok' :
                (entry.action === 'dry_run' ? '' : 'log-fail');
            html += '<div class="action-log-entry ' + cls + '">' +
                '<span class="log-time">' + _escape(entry.time) + '</span> ' +
                '<span class="log-icon">' + icon + '</span> ' +
                _escape(entry.action) + ' ' +
                _escape(entry.chapter) + ' &rarr; ' +
                '<strong>' + _escape(entry.platform) + '</strong>';
            if (entry.url) {
                html += ' <a href="' + _escape(entry.url) +
                    '" target="_blank" rel="noopener">&nearr;</a>';
            }
            if (entry.queued) html += ' (queued for desktop)';
            if (entry.error && !entry.ok) {
                html += ' <span class="log-error">' +
                    _escape(entry.error) + '</span>';
            }
            html += '</div>';
        }
        html += '</div>';
        el.innerHTML = html;
    }

    // ── Phase 6d: Bulk publish ─────────────────────────────────
    //
    // Three entry points: row-end button, "Publish all new", "Update
    // all drifted". Each collects actionable targets from the matrix
    // data, shows a preflight dialog, then runs sequential requests.

    let _lastMatrixData = null;  // cached from _render for bulk target collection
    let _actionLog = [];         // per-session action history (Phase 6e)

    function _collectTargets(mode, chIdx) {
        if (!_lastMatrixData) return [];
        const targets = [];
        for (const row of _lastMatrixData.matrix) {
            if (mode === 'row' && row.chapter_index !== chIdx) continue;
            for (const p of _lastMatrixData.platforms) {
                const cell = row.cells[p.id];
                if (!cell) continue;
                if (mode === 'all_new' || mode === 'row') {
                    if (cell.status === 'ready' || cell.status === 'ready_retry' || cell.status === 'deleted_upstream') {
                        targets.push({
                            platId: p.id, platName: p.name,
                            chIdx: row.chapter_index, chTitle: row.chapter_title,
                            action: 'post', cell: cell,
                        });
                    }
                }
                if (mode === 'all_drifted' || mode === 'row') {
                    if (cell.status === 'posted_drifted') {
                        targets.push({
                            platId: p.id, platName: p.name,
                            chIdx: row.chapter_index, chTitle: row.chapter_title,
                            action: 'update', cell: cell,
                        });
                    }
                }
            }
        }
        return targets;
    }

    function _openBulkPreflight(targets, mode) {
        if (!targets.length) return;
        const storyName = _currentStory;
        if (!storyName) return;

        const overlay = document.createElement('div');
        overlay.className = 'bulk-preflight-overlay';

        const modeLabel = mode === 'all_new' ? 'Publish all new'
            : mode === 'all_drifted' ? 'Update all drifted'
            : 'Publish row';

        let html = '<div class="bulk-preflight-dialog">';
        html += '<div class="bulk-preflight-header">';
        html += '<strong>' + modeLabel + '</strong> — ' + targets.length + ' target(s)';
        html += '<button class="bulk-preflight-close">&times;</button>';
        html += '</div>';

        html += '<div class="bulk-preflight-body">';
        html += '<div class="bulk-preflight-list">';
        for (let i = 0; i < targets.length; i++) {
            const t = targets[i];
            const verb = t.action === 'post' ? 'Post' : 'Update';
            html += '<label class="bulk-target-row">' +
                '<input type="checkbox" data-idx="' + i + '" checked> ' +
                '<span class="bulk-target-verb">' + verb + '</span> ' +
                _escape(t.chTitle || 'Full story') + ' → ' +
                '<strong>' + _escape(t.platName) + '</strong>' +
                '</label>';
        }
        html += '</div>';

        html += '<div class="bulk-preflight-options">';
        html += '<label><input type="checkbox" id="bulk-draft" checked> Save as draft</label>';
        html += '</div>';
        html += '</div>';

        html += '<div class="bulk-preflight-footer">';
        html += '<button class="btn btn-sm btn-outline" data-bulk="cancel">Cancel</button>';
        html += '<button class="btn btn-sm btn-outline" data-bulk="dry_run">Dry Run All</button>';
        html += '<button class="btn btn-sm btn-primary" data-bulk="go">' +
            modeLabel + ' (' + targets.length + ')</button>';
        html += '</div></div>';

        overlay.innerHTML = html;
        document.body.appendChild(overlay);

        // Update count when checkboxes change
        const updateCount = () => {
            const checked = overlay.querySelectorAll('.bulk-target-row input:checked').length;
            const goBtn = overlay.querySelector('[data-bulk="go"]');
            if (goBtn) goBtn.textContent = modeLabel + ' (' + checked + ')';
        };
        overlay.querySelectorAll('.bulk-target-row input').forEach(cb => {
            cb.addEventListener('change', updateCount);
        });

        overlay.querySelector('.bulk-preflight-close').addEventListener('click', () => {
            overlay.remove();
        });
        overlay.querySelector('[data-bulk="cancel"]').addEventListener('click', () => {
            overlay.remove();
        });
        overlay.querySelector('[data-bulk="dry_run"]').addEventListener('click', () => {
            const selected = _getSelectedTargets(overlay, targets);
            if (!selected.length) return;
            overlay.remove();
            _runBulk(storyName, selected, true);
        });
        overlay.querySelector('[data-bulk="go"]').addEventListener('click', () => {
            const selected = _getSelectedTargets(overlay, targets);
            if (!selected.length) return;
            const draft = overlay.querySelector('#bulk-draft')?.checked ?? true;
            const ok = confirm(
                modeLabel + ': ' + selected.length + ' item(s)' +
                (draft ? ' as DRAFT' : ' LIVE') +
                '.\n\nThis will make real requests to external platforms.'
            );
            if (!ok) return;
            overlay.remove();
            _runBulk(storyName, selected, false, draft);
        });
    }

    function _getSelectedTargets(overlay, targets) {
        const selected = [];
        overlay.querySelectorAll('.bulk-target-row input:checked').forEach(cb => {
            const idx = parseInt(cb.dataset.idx);
            if (targets[idx]) selected.push(targets[idx]);
        });
        return selected;
    }

    let _bulkAborted = false;

    async function _runBulk(storyName, targets, dryRun, draft) {
        _bulkAborted = false;

        // Replace the detail panel with a progress view
        const body = document.getElementById('publish-check-body');
        if (!body) return;

        let html = '<div class="bulk-progress">';
        html += '<div class="bulk-progress-header" id="bulk-progress-header">' +
            (dryRun ? 'Dry run' : 'Publishing') + ' 0/' + targets.length +
            '</div>';
        html += '<div class="bulk-progress-list" id="bulk-progress-list">';
        for (let i = 0; i < targets.length; i++) {
            const t = targets[i];
            const verb = t.action === 'post' ? 'Post' : 'Update';
            html += '<div class="bulk-progress-item" id="bulk-item-' + i + '">' +
                '<span class="bulk-item-status">⏳</span> ' +
                verb + ' ' + _escape(t.chTitle || 'Full story') +
                ' → ' + _escape(t.platName) +
                '<span class="bulk-item-result" id="bulk-result-' + i + '"></span>' +
                '</div>';
        }
        html += '</div>';
        html += '<div class="bulk-progress-footer">';
        if (!dryRun) {
            html += '<button class="btn btn-sm" id="bulk-cancel-btn">Cancel remaining</button>';
        }
        html += '<button class="btn btn-sm btn-outline" id="bulk-close-btn" disabled>Close & refresh</button>';
        html += '</div></div>';

        body.innerHTML = html;

        if (!dryRun) {
            document.getElementById('bulk-cancel-btn')?.addEventListener('click', () => {
                _bulkAborted = true;
                const btn = document.getElementById('bulk-cancel-btn');
                if (btn) { btn.disabled = true; btn.textContent = 'Cancelling...'; }
            });
        }

        let succeeded = 0, failed = 0, skipped = 0;

        for (let i = 0; i < targets.length; i++) {
            if (_bulkAborted) {
                skipped += targets.length - i;
                for (let j = i; j < targets.length; j++) {
                    _updateBulkItem(j, '⊘', 'Cancelled');
                }
                break;
            }

            const t = targets[i];
            _updateBulkItem(i, '⏳', 'Working...');
            _updateBulkHeader(i, targets.length, succeeded, failed);

            try {
                const action = dryRun ? 'dry_run' : t.action;
                const resp = await fetch(
                    '/api/editor/stories/' + encodeURIComponent(storyName) + '/publish',
                    {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({
                            platform: t.platId,
                            chapter: t.chIdx,
                            action: action,
                            draft: draft ?? true,
                            confirm_live: !dryRun,
                        }),
                    }
                );
                const data = await resp.json();

                if (dryRun) {
                    _updateBulkItem(i, data.ok ? '✓' : '✗',
                        data.ok ? 'OK' : (data.errors || []).join('; '));
                    if (data.ok) succeeded++; else failed++;
                } else if (data.ok) {
                    const url = data.results?.[0]?.external_url;
                    const queued = data.results?.[0]?.queued_desktop;
                    _updateBulkItem(i, '✓',
                        queued ? 'Queued for desktop' :
                        url ? '<a href="' + _escape(url) + '" target="_blank">Posted</a>' : 'Done');
                    succeeded++;
                } else {
                    const err = data.results?.[0]?.error || 'Failed';
                    _updateBulkItem(i, '✗', _escape(err));
                    failed++;
                }
            } catch (e) {
                _updateBulkItem(i, '✗', _escape(e.message));
                failed++;
            }
        }

        _updateBulkHeader(targets.length, targets.length, succeeded, failed, skipped);

        _logAction(
            dryRun ? 'bulk_dry_run' : 'bulk',
            targets.length + ' targets',
            succeeded + ' ok, ' + failed + ' failed' + (skipped ? ', ' + skipped + ' skipped' : ''),
            { ok: failed === 0 && skipped === 0 }
        );

        const closeBtn = document.getElementById('bulk-close-btn');
        if (closeBtn) {
            closeBtn.disabled = false;
            closeBtn.addEventListener('click', () => {
                if (_currentStory === storyName) load(storyName);
            });
        }
        const cancelBtn = document.getElementById('bulk-cancel-btn');
        if (cancelBtn) cancelBtn.style.display = 'none';
    }

    function _updateBulkItem(idx, icon, text) {
        const item = document.getElementById('bulk-item-' + idx);
        if (!item) return;
        const status = item.querySelector('.bulk-item-status');
        const result = document.getElementById('bulk-result-' + idx);
        if (status) status.textContent = icon;
        if (result) result.innerHTML = ' — ' + text;
        item.className = 'bulk-progress-item ' +
            (icon === '✓' ? 'bulk-success' : icon === '✗' ? 'bulk-fail' : '');
    }

    function _updateBulkHeader(current, total, ok, fail, skip) {
        const h = document.getElementById('bulk-progress-header');
        if (!h) return;
        h.innerHTML = 'Progress: ' + current + '/' + total +
            ' — <span class="stat-ready">' + ok + ' succeeded</span>' +
            (fail ? ', <span class="stat-blocked">' + fail + ' failed</span>' : '') +
            (skip ? ', ' + skip + ' cancelled' : '');
    }

    // ── Phase 6f: Scheduling helpers ────────────────────────────

    function _toLocalISOString(date) {
        // Format as YYYY-MM-DDTHH:MM for datetime-local input
        const pad = n => String(n).padStart(2, '0');
        return date.getFullYear() + '-' + pad(date.getMonth() + 1) + '-' +
            pad(date.getDate()) + 'T' + pad(date.getHours()) + ':' +
            pad(date.getMinutes());
    }

    // ── Drip scheduling (gap G1, gap-wave-2 §3) ──────────────────────────────
    // "Post chapter 1 at T, then one chapter every N days." Inline panel above
    // the matrix; the local preview mirrors what the server computes (server is
    // authoritative + validates every chapter × platform before enqueuing).
    function _toggleDripPanel() {
        const existing = document.getElementById('drip-panel');
        if (existing) { existing.remove(); return; }
        const body = document.getElementById('publish-check-body');
        if (!body || !_lastMatrixData) return;
        const chapters = _lastMatrixData.matrix.filter(r => r.chapter_index > 0);
        if (!chapters.length) {
            if (window.toast) window.toast.info('Drip scheduling needs a chaptered story');
            return;
        }
        const plats = _lastMatrixData.platforms.map(p =>
            '<label style="font-size:12px;white-space:nowrap"><input type="checkbox" class="drip-plat-check" value="' +
            _escape(p.id) + '"> ' + _escape(p.name) + '</label>').join(' ');
        // Default start: tomorrow 20:00 local.
        const start = new Date();
        start.setDate(start.getDate() + 1);
        start.setHours(20, 0, 0, 0);
        const pad = n => String(n).padStart(2, '0');
        const startVal = start.getFullYear() + '-' + pad(start.getMonth() + 1) + '-' + pad(start.getDate()) +
            'T' + pad(start.getHours()) + ':' + pad(start.getMinutes());
        const panel = document.createElement('div');
        panel.id = 'drip-panel';
        panel.className = 'card';
        panel.style.cssText = 'margin-bottom:12px;padding:12px';
        panel.innerHTML =
            '<h3 style="margin:0 0 6px">💧 Drip schedule — ' + chapters.length + ' chapters</h3>' +
            '<p class="muted" style="font-size:12px;margin:0 0 8px">Chapter 1 posts at the start time, then one ' +
            'chapter every N days at the same time. Every chapter is validated first; the rows land in ' +
            'Queue &amp; Schedule and the whole drip can be cancelled as one.</p>' +
            '<div style="display:flex;flex-wrap:wrap;gap:10px;align-items:center;margin-bottom:8px">' + plats + '</div>' +
            '<div style="display:flex;flex-wrap:wrap;gap:10px;align-items:center">' +
            '<label style="font-size:12px">Start <input type="datetime-local" id="drip-start" value="' + startVal + '"></label>' +
            '<label style="font-size:12px">every <input type="number" id="drip-interval" min="1" max="60" value="7" style="width:56px"> day(s)</label>' +
            '<button class="btn btn-sm btn-primary" id="drip-go">Start drip</button>' +
            '<button class="btn btn-sm btn-outline" id="drip-cancel">Cancel</button>' +
            '<span id="drip-msg" class="muted" style="font-size:12px"></span>' +
            '</div>' +
            '<div id="drip-preview" class="muted" style="font-size:12px;margin-top:8px"></div>';
        body.prepend(panel);
        const upd = () => _updateDripPreview(chapters);
        panel.querySelector('#drip-start').addEventListener('input', upd);
        panel.querySelector('#drip-interval').addEventListener('input', upd);
        panel.querySelector('#drip-cancel').addEventListener('click', () => panel.remove());
        panel.querySelector('#drip-go').addEventListener('click', () => _submitDrip());
        upd();
    }

    function _updateDripPreview(chapters) {
        const box = document.getElementById('drip-preview');
        const startVal = document.getElementById('drip-start')?.value;
        const days = parseInt(document.getElementById('drip-interval')?.value, 10) || 7;
        if (!box) return;
        const start = new Date(startVal);
        if (!startVal || isNaN(start.getTime())) { box.textContent = ''; return; }
        box.innerHTML = chapters.map((r, i) => {
            const d = new Date(start.getTime() + i * days * 86400000);
            return 'Ch ' + r.chapter_index + ' → ' + _escape(d.toLocaleString());
        }).join(' &nbsp;·&nbsp; ');
    }

    async function _submitDrip() {
        const msg = document.getElementById('drip-msg');
        const platforms = Array.from(document.querySelectorAll('.drip-plat-check:checked')).map(c => c.value);
        const startVal = document.getElementById('drip-start')?.value;
        const days = parseInt(document.getElementById('drip-interval')?.value, 10);
        if (!platforms.length) { if (msg) msg.textContent = 'Pick at least one platform.'; return; }
        const start = new Date(startVal);
        if (!startVal || isNaN(start.getTime())) { if (msg) msg.textContent = 'Invalid start time.'; return; }
        // A drip is chapters × platforms in one click and a mis-set interval
        // schedules a month of public posts, so it gets the dialog even though
        // a single schedule does not (spec §10 Q4).
        if (window.Components && !(await Components.confirmPublish({
            title: _currentStory.replace(/_/g, ' '), subtitle: 'Drip campaign',
            verb: 'Schedule', noun: 'sites',
            warning: 'Every unposted chapter to each site, one slot every ' + (days || 7)
                + ' day(s), starting ' + start.toLocaleString() + '. Cancellable as a unit from Queue & Schedule.',
            targets: platforms.map(code => {
                const p = (window.platformByCode && window.platformByCode(code)) || { label: code, emoji: '' };
                return { code, label: p.label, emoji: p.emoji };
            }),
        }))) { if (msg) msg.textContent = 'Drip not scheduled.'; return; }
        if (msg) msg.textContent = 'Validating every chapter…';
        try {
            const resp = await fetch('/api/editor/stories/' + encodeURIComponent(_currentStory) + '/drip', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ platforms, start: start.toISOString(), interval_days: days || 7 }),
            });
            const data = await resp.json();
            if (!resp.ok) throw new Error(data.detail || 'HTTP ' + resp.status);
            document.getElementById('drip-panel')?.remove();
            if (window.toast) window.toast.success(
                '💧 Drip scheduled — ' + data.rows + ' item(s) across ' + data.slots.length +
                ' slot(s). See Queue & Schedule.');
        } catch (err) {
            if (msg) msg.textContent = 'Failed: ' + (err.message || err);
        }
    }

    async function _submitSchedule(action, platId, platName, chIdx, chTitle, datetimeLocalVal) {
        const storyName = _currentStory;
        if (!storyName) return;

        const draftCb = document.getElementById('publish-opt-draft');
        const draft = draftCb ? draftCb.checked : true;
        const resultBox = document.getElementById('publish-action-result');

        // Convert local datetime-local value to ISO 8601 with timezone
        const localDate = new Date(datetimeLocalVal);
        if (isNaN(localDate.getTime())) {
            if (resultBox) {
                resultBox.innerHTML = '<div class="publish-action-error">Invalid date/time.</div>';
            }
            return;
        }
        const isoStr = localDate.toISOString();

        if (resultBox) {
            resultBox.innerHTML = '<div class="publish-action-loading">Scheduling...</div>';
        }

        try {
            const resp = await fetch(
                '/api/editor/stories/' + encodeURIComponent(storyName) + '/schedule',
                {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({
                        platform: platId,
                        chapter: chIdx,
                        action: action,
                        scheduled_at: isoStr,
                        draft: draft,
                    }),
                }
            );
            const data = await resp.json();
            if (!resp.ok) {
                throw new Error(data.detail || 'HTTP ' + resp.status);
            }
            if (resultBox) {
                const when = new Date(data.scheduled_at + 'Z');
                resultBox.innerHTML =
                    '<div class="publish-action-success">' +
                    '<strong>Scheduled!</strong> ' +
                    _escape(action) + ' to ' + _escape(platName) +
                    ' at ' + when.toLocaleString() +
                    ' (queue #' + data.queue_id + ')' +
                    '</div>';
            }
            // Hide the form
            const schedForm = document.getElementById('schedule-form');
            if (schedForm) schedForm.style.display = 'none';

            // Refresh the scheduled items list
            _loadScheduledItems(platId, chIdx);

            if (window.toast) {
                const when = new Date(data.scheduled_at + 'Z');
                window.toast.success(
                    `Scheduled: ${chTitle || 'Full story'} → ${platName} at ${when.toLocaleString()}`
                );
            }
            _logAction('schedule', platName, chTitle || 'Full story', { ok: true });
        } catch (e) {
            if (resultBox) {
                resultBox.innerHTML =
                    '<div class="publish-action-error">Schedule failed: ' +
                    _escape(e.message) + '</div>';
            }
            if (window.toast) {
                window.toast.error(`Schedule failed: ${e.message || e}`);
            }
            _logAction('schedule', platName, chTitle || 'Full story', { ok: false, error: e.message });
        }
    }

    async function _loadScheduledItems(platId, chIdx) {
        const container = document.getElementById('schedule-pending');
        if (!container) return;
        const storyName = _currentStory;
        if (!storyName) return;

        try {
            const resp = await fetch(
                '/api/editor/stories/' + encodeURIComponent(storyName) + '/scheduled'
            );
            if (!resp.ok) return;
            const data = await resp.json();

            // Filter to items matching this cell
            const items = (data.items || []).filter(
                i => i.platform === platId && i.chapter_index === chIdx
            );

            if (!items.length) {
                container.innerHTML = '';
                return;
            }

            // Header includes a bulk-cancel button when more than one
            // scheduled item exists for this cell. Backend's
            // cancel_all_for already understands platform+chapter scoping.
            let html = '<div class="schedule-pending-header">';
            html += '<span>Scheduled:</span>';
            if (items.length > 1) {
                html += ' <button class="btn btn-xs btn-outline schedule-cancel-all-btn">' +
                    'Cancel all (' + items.length + ')</button>';
            }
            html += '</div>';

            for (const item of items) {
                const when = item.scheduled_at
                    ? new Date(item.scheduled_at + 'Z').toLocaleString()
                    : 'Immediate';
                const statusCls = item.status === 'processing' ? 'schedule-processing' : '';
                html += '<div class="schedule-pending-item ' + statusCls + '">' +
                    '<span class="schedule-pending-icon">&#128340;</span> ' +
                    _escape(item.action) + ' — ' + when +
                    ' <span class="schedule-pending-status">(' + _escape(item.status) + ')</span>';
                // Backend's cancel_queue_item (v2.20.3+) accepts
                // pending/processing/failed — surface Cancel for every
                // status the backend can actually act on.
                const cancellableStatuses = ['pending', 'processing', 'failed'];
                if (cancellableStatuses.includes(item.status)) {
                    html += ' <button class="btn btn-xs btn-outline schedule-cancel-btn" ' +
                        'data-queue-id="' + item.queue_id + '">Cancel</button>';
                }
                html += '</div>';
            }
            container.innerHTML = html;

            // Bind per-row cancel buttons
            container.querySelectorAll('.schedule-cancel-btn').forEach(btn => {
                btn.addEventListener('click', async () => {
                    const queueId = btn.dataset.queueId;
                    btn.disabled = true;
                    btn.textContent = '...';
                    try {
                        const resp = await fetch(
                            '/api/editor/stories/' + encodeURIComponent(storyName) +
                            '/scheduled/' + queueId,
                            { method: 'DELETE' }
                        );
                        if (!resp.ok) {
                            const d = await resp.json();
                            throw new Error(d.detail || 'HTTP ' + resp.status);
                        }
                        _loadScheduledItems(platId, chIdx);
                    } catch (e) {
                        btn.textContent = 'Error';
                        setTimeout(() => { btn.textContent = 'Cancel'; btn.disabled = false; }, 2000);
                    }
                });
            });

            // Bind bulk cancel-all button
            const cancelAllBtn = container.querySelector('.schedule-cancel-all-btn');
            if (cancelAllBtn) {
                cancelAllBtn.addEventListener('click', async () => {
                    if (!confirm('Cancel all ' + items.length + ' scheduled item(s) for this cell?')) {
                        return;
                    }
                    cancelAllBtn.disabled = true;
                    cancelAllBtn.textContent = 'Cancelling...';
                    try {
                        const qs = '?platform=' + encodeURIComponent(platId) +
                            '&chapter=' + encodeURIComponent(chIdx);
                        const resp = await fetch(
                            '/api/editor/stories/' + encodeURIComponent(storyName) +
                            '/scheduled' + qs,
                            { method: 'DELETE' }
                        );
                        if (!resp.ok) {
                            const d = await resp.json();
                            throw new Error(d.detail || 'HTTP ' + resp.status);
                        }
                        _loadScheduledItems(platId, chIdx);
                    } catch (e) {
                        cancelAllBtn.textContent = 'Error';
                        setTimeout(() => {
                            cancelAllBtn.textContent = 'Cancel all (' + items.length + ')';
                            cancelAllBtn.disabled = false;
                        }, 2000);
                    }
                });
            }
        } catch (e) {
            // Silently fail — scheduled display is supplementary
        }
    }

    return { open, close };
})();
