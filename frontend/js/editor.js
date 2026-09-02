/**
 * Story Editor — edit MASTER.md with CodeMirror, live format preview,
 * chapter navigation, auto-save recovery, and per-chapter word count.
 */
const Editor = {
    // State
    storyName: null,
    lastSavedContent: '',
    lastMtime: 0,
    previewFormat: 'clean_html',
    previewDebounceTimer: null,
    previewRequestId: 0,
    slopScore: null,
    isDirty: false,
    chapters: [],
    hiddenPanels: new Set(),
    _syncingScroll: false,
    cmView: null,           // CodeMirror EditorView instance (MD source)
    cmSourceView: null,     // CodeMirror for format source (read-only)
    cmCssView: null,        // CodeMirror for CSS editor
    autoSaveTimer: null,    // localStorage auto-save interval
    // WYSIWYG state
    _wysiwygEditSource: null,   // 'cm' | 'wysiwyg' | null — prevents sync loops
    _wysiwygSyncTimer: null,    // debounce for WYSIWYG→CM conversion
    _turndown: null,            // TurndownService instance
    _frontMatterMd: '',         // cached front matter (above <!-- @body -->)
    _bodyStartLine: 0,          // line index of <!-- @body -->

    // ---------------------------------------------------------------------------
    // Story list page
    // ---------------------------------------------------------------------------

    async renderStoryList() {
        App._setContent('<div class="loading-spinner">Loading stories...</div>');
        try {
            const resp = await fetch('/api/editor/stories');
            const data = await resp.json();
            const stories = data.stories || [];

            const cards = stories.map(s => {
                const wc = s.word_count ? `${(s.word_count / 1000).toFixed(1)}K words` : 'no word count';
                const ch = s.chapters ? `${s.chapters} ch` : '';
                const hasMaster = s.has_master ? '' : '<span style="color:var(--color-error)">No MASTER.md</span>';
                // Card is the link; the delete button sits in the corner with
                // its own click handler that stops propagation so it doesn't
                // also navigate into the editor.
                return `
                    <div class="stat-card story-card" style="position:relative;padding-right:36px">
                        <a href="#/editor/${s.name}" style="text-decoration:none;color:inherit;cursor:pointer;display:block">
                            <h4>${Utils.escapeHtml(s.title)}</h4>
                            <p style="color:var(--text-secondary);font-size:0.85rem">${wc}${ch ? ' · ' + ch : ''} ${hasMaster}</p>
                        </a>
                        <button class="story-delete-btn" data-story="${Utils.escapeHtml(s.name)}" title="Delete story" style="position:absolute;top:8px;right:8px;background:transparent;border:1px solid var(--border);border-radius:var(--radius-sm);padding:2px 8px;font-size:14px;cursor:pointer;color:var(--text-muted);line-height:1">&#x1F5D1;</button>
                    </div>`;
            }).join('');

            App._setContent(`
                <div class="page-header">
                    <h2>Story Editor</h2>
                    <p class="subtitle">Select a story to edit MASTER.md and preview in all formats</p>
                </div>
                <div style="margin-bottom:16px;display:flex;gap:10px;flex-wrap:wrap">
                    <button class="btn btn-sm" id="create-story-btn">+ Create New Story</button>
                    <button class="btn btn-sm btn-outline" id="import-story-btn">Import from Platform</button>
                    <button class="btn btn-sm btn-outline" id="regen-all-btn" title="Rebuild every story's derived format files from its MASTER.md">↻ Regenerate All</button>
                </div>
                <div class="card-grid">${cards || '<p>No stories found in the archive.</p>'}</div>

                <div class="import-overlay" id="import-overlay">
                    <div class="import-dialog">
                        <div class="import-dialog-header">
                            <h3>Import from Platform</h3>
                            <button class="import-close-btn" id="import-close-btn" title="Close">&times;</button>
                        </div>
                        <p class="import-subtitle">Import existing stories from your polled platforms into the local archive.</p>
                        <div class="import-manual">
                            <label class="import-manual-label">
                                <span>Import by URL or ID (works for drafts too)</span>
                                <input type="text" id="import-manual-input" class="import-manual-input" placeholder="https://archiveofourown.org/works/12345 or sf:67890" autocomplete="off">
                            </label>
                            <button class="btn btn-sm" id="import-manual-btn">Import</button>
                        </div>
                        <div id="import-manual-status" class="import-manual-status"></div>
                        <div id="import-content">
                            <div class="loading-spinner">Loading available submissions...</div>
                        </div>
                    </div>
                </div>

                <div class="create-story-overlay" id="create-story-overlay">
                    <div class="create-story-dialog">
                        <h3>Create New Story</h3>
                        <label class="create-story-label">
                            Title
                            <input type="text" id="create-story-title" class="create-story-input" placeholder="My New Story" autocomplete="off">
                        </label>
                        <label class="create-story-label">
                            Folder name
                            <input type="text" id="create-story-name" class="create-story-input" placeholder="My_New_Story" autocomplete="off">
                            <span class="create-story-hint">Letters, digits, and underscores only</span>
                        </label>
                        <label class="create-story-label">
                            Author
                            <input type="text" id="create-story-author" class="create-story-input" placeholder="Author name" autocomplete="off">
                        </label>
                        <label class="create-story-label">
                            Genre template <span class="create-story-hint">(optional — pre-fills tags + rating)</span>
                            <select id="create-story-genre" class="create-story-input">
                                <option value="">None</option>
                                <option value="romance">Romance</option>
                                <option value="erotica">Erotica</option>
                                <option value="adventure">Adventure</option>
                                <option value="comedy">Comedy</option>
                                <option value="drama">Drama</option>
                                <option value="fantasy">Fantasy</option>
                                <option value="sci_fi">Sci-Fi</option>
                                <option value="slice_of_life">Slice of Life</option>
                                <option value="horror">Horror</option>
                            </select>
                        </label>
                        <div style="display:flex;gap:12px">
                            <label class="create-story-label" style="flex:1">
                                Chapters
                                <select id="create-story-chapters" class="create-story-input">
                                    ${Array.from({length: 20}, (_, i) => `<option value="${i+1}"${i === 0 ? ' selected' : ''}>${i+1}</option>`).join('')}
                                </select>
                            </label>
                            <label class="create-story-label" style="flex:1">
                                Rating
                                <select id="create-story-rating" class="create-story-input">
                                    <option value="general">General</option>
                                    <option value="mature">Mature</option>
                                    <option value="explicit" selected>Explicit</option>
                                </select>
                            </label>
                        </div>
                        <label class="create-story-label">
                            Import content from file <span class="create-story-hint">(optional — replaces template)</span>
                            <input type="file" id="create-story-file" class="create-story-input" accept=".md,.txt,.html,.htm,.bbcode,.rtf">
                            <span class="create-story-hint">.md, .txt, .html, .bbcode, .rtf</span>
                        </label>
                        <div id="create-story-error" class="create-story-error" style="display:none"></div>
                        <div class="create-story-actions">
                            <button class="btn btn-sm btn-outline" id="create-story-cancel">Cancel</button>
                            <button class="btn btn-sm" id="create-story-submit">Create</button>
                        </div>
                    </div>
                </div>

                <div class="create-story-overlay" id="delete-story-overlay">
                    <div class="create-story-dialog">
                        <h3 style="color:var(--danger)">Delete Story</h3>
                        <p style="color:var(--text-secondary);font-size:0.9rem;margin-bottom:8px">
                            This permanently deletes the story folder and every file in it (Markdown, BBCode, HTML, EPUB, PDFs, covers, chapter thumbnails, backups). It cannot be undone.
                        </p>
                        <p style="color:var(--text-muted);font-size:0.85rem;margin-bottom:12px">
                            Posting history (Publications and queue items) is retained — only the local files are removed.
                        </p>
                        <div id="delete-story-target" style="font-size:0.95rem;margin-bottom:8px"></div>
                        <label class="create-story-label">
                            Type the folder name to confirm
                            <input type="text" id="delete-story-confirm-input" class="create-story-input" placeholder="" autocomplete="off">
                        </label>
                        <div id="delete-story-error" class="create-story-error" style="display:none"></div>
                        <div class="create-story-actions">
                            <button class="btn btn-sm btn-outline" id="delete-story-cancel">Cancel</button>
                            <button class="btn btn-sm btn-danger" id="delete-story-submit" disabled>Delete</button>
                        </div>
                    </div>
                </div>

                <div class="create-story-overlay" id="regen-all-overlay">
                    <div class="create-story-dialog" style="max-width:760px;width:90vw">
                        <h3>Regenerate All Stories</h3>
                        <p style="color:var(--text-secondary);font-size:0.9rem;margin-bottom:12px">
                            Rebuilds every derived format file (Markdown chapters, BBCode, Clean HTML,
                            SoFurry HTML, Styled HTML, SquidgeWorld, EPUB, optionally PDF) for every
                            story in the archive from its <code>MASTER.md</code>. Existing files are
                            overwritten. Word counts and story.json metadata are preserved.
                        </p>
                        <label class="create-story-label" style="display:flex;align-items:center;gap:8px;margin-bottom:8px">
                            <input type="checkbox" id="regen-all-skip-pdf" checked style="width:auto;margin:0">
                            Skip PDF (recommended — adds ~30s per story)
                        </label>
                        <div id="regen-all-progress" style="display:none">
                            <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:6px">
                                <span id="regen-all-counts" style="font-weight:600;font-size:0.92rem"></span>
                                <span id="regen-all-elapsed" style="color:var(--text-secondary);font-size:0.85rem"></span>
                            </div>
                            <div style="background:var(--bg-tertiary);border-radius:var(--radius-sm);height:8px;overflow:hidden;margin-bottom:10px">
                                <div id="regen-all-bar" style="background:var(--accent);height:100%;width:0%;transition:width .2s"></div>
                            </div>
                            <pre id="regen-all-log" style="background:var(--bg-tertiary);border:1px solid var(--border);border-radius:var(--radius-sm);padding:10px;max-height:340px;overflow-y:auto;font-family:var(--font-mono,monospace);font-size:12px;line-height:1.5;margin:0;white-space:pre-wrap;word-break:break-word"></pre>
                        </div>
                        <div id="regen-all-actions" class="create-story-actions" style="margin-top:14px">
                            <button class="btn btn-sm btn-outline" id="regen-all-close">Close</button>
                            <button class="btn btn-sm btn-outline" id="regen-all-cancel" style="display:none">Cancel</button>
                            <button class="btn btn-sm" id="regen-all-start">Start</button>
                        </div>
                    </div>
                </div>
            `);

            // Bind create-story dialog
            const overlay = document.getElementById('create-story-overlay');
            const titleInput = document.getElementById('create-story-title');
            const nameInput = document.getElementById('create-story-name');

            // Genre template rating map — keeps frontend in sync with backend GENRE_TEMPLATES
            const genreRatings = {
                romance: 'mature', erotica: 'explicit', adventure: 'general',
                comedy: 'general', drama: 'mature', fantasy: 'general',
                sci_fi: 'general', slice_of_life: 'general', horror: 'mature',
            };

            document.getElementById('create-story-btn').addEventListener('click', () => {
                overlay.classList.add('open');
                titleInput.value = '';
                nameInput.value = '';
                document.getElementById('create-story-author').value = '';
                document.getElementById('create-story-genre').value = '';
                document.getElementById('create-story-chapters').value = '1';
                document.getElementById('create-story-rating').value = 'explicit';
                document.getElementById('create-story-error').style.display = 'none';
                titleInput.focus();
            });

            // Auto-generate folder name from title
            titleInput.addEventListener('input', () => {
                nameInput.value = titleInput.value.trim().replace(/[^A-Za-z0-9_ ]/g, '').replace(/ +/g, '_');
            });

            // When genre changes, auto-update rating to the template default
            document.getElementById('create-story-genre').addEventListener('change', (e) => {
                const genre = e.target.value;
                if (genre && genreRatings[genre]) {
                    document.getElementById('create-story-rating').value = genreRatings[genre];
                }
            });

            document.getElementById('create-story-cancel').addEventListener('click', () => {
                overlay.classList.remove('open');
            });
            overlay.addEventListener('click', (e) => {
                if (e.target === overlay) overlay.classList.remove('open');
            });

            document.getElementById('create-story-submit').addEventListener('click', () => {
                Editor._submitCreateStory();
            });
            // Enter key submits
            overlay.addEventListener('keydown', (e) => {
                if (e.key === 'Enter') { e.preventDefault(); Editor._submitCreateStory(); }
                if (e.key === 'Escape') overlay.classList.remove('open');
            });

            // Bind delete-story overlay.  Two layers of confirmation: the
            // user must (1) type the folder name into the input to enable
            // the Delete button, and (2) acknowledge the native confirm()
            // dialog before the DELETE request fires.
            const deleteOverlay = document.getElementById('delete-story-overlay');
            const deleteInput = document.getElementById('delete-story-confirm-input');
            const deleteSubmit = document.getElementById('delete-story-submit');
            const deleteTarget = document.getElementById('delete-story-target');
            const deleteError = document.getElementById('delete-story-error');
            // Holds the full story name (including any "Parent/Sub" prefix
            // for versioned stories) of the card whose delete button was
            // clicked.  The leaf folder name is what the user has to type.
            Editor._pendingDelete = null;

            document.querySelectorAll('.story-delete-btn').forEach(btn => {
                btn.addEventListener('click', (e) => {
                    e.preventDefault();
                    e.stopPropagation();
                    const fullName = btn.getAttribute('data-story') || '';
                    const leaf = fullName.split('/').pop();
                    Editor._pendingDelete = { fullName, leaf };
                    deleteTarget.innerHTML = `Folder: <code style="background:var(--bg-tertiary);padding:2px 6px;border-radius:3px">${Utils.escapeHtml(fullName)}</code>`;
                    deleteInput.value = '';
                    deleteInput.placeholder = leaf;
                    deleteSubmit.disabled = true;
                    deleteError.style.display = 'none';
                    deleteOverlay.classList.add('open');
                    setTimeout(() => deleteInput.focus(), 50);
                });
            });

            deleteInput.addEventListener('input', () => {
                deleteSubmit.disabled = !Editor._pendingDelete || deleteInput.value !== Editor._pendingDelete.leaf;
            });
            document.getElementById('delete-story-cancel').addEventListener('click', () => {
                deleteOverlay.classList.remove('open');
                Editor._pendingDelete = null;
            });
            deleteOverlay.addEventListener('click', (e) => {
                if (e.target === deleteOverlay) {
                    deleteOverlay.classList.remove('open');
                    Editor._pendingDelete = null;
                }
            });
            deleteOverlay.addEventListener('keydown', (e) => {
                if (e.key === 'Escape') {
                    deleteOverlay.classList.remove('open');
                    Editor._pendingDelete = null;
                }
                if (e.key === 'Enter' && !deleteSubmit.disabled) {
                    e.preventDefault();
                    deleteSubmit.click();
                }
            });
            deleteSubmit.addEventListener('click', async () => {
                if (!Editor._pendingDelete) return;
                const { fullName, leaf } = Editor._pendingDelete;
                if (!confirm(`Really delete "${fullName}"? Every file in the folder will be removed permanently.`)) return;
                deleteSubmit.disabled = true;
                deleteSubmit.textContent = 'Deleting...';
                deleteError.style.display = 'none';
                try {
                    const url = `/api/editor/stories/${encodeURI(fullName)}?confirm_name=${encodeURIComponent(leaf)}`;
                    const resp = await fetch(url, { method: 'DELETE' });
                    if (!resp.ok) {
                        let detail = `HTTP ${resp.status}`;
                        try { detail = (await resp.json()).detail || detail; } catch {}
                        throw new Error(detail);
                    }
                    deleteOverlay.classList.remove('open');
                    Editor._pendingDelete = null;
                    Editor.renderStoryList();
                } catch (err) {
                    deleteError.textContent = err.message;
                    deleteError.style.display = 'block';
                    deleteSubmit.disabled = false;
                    deleteSubmit.textContent = 'Delete';
                }
            });

            // Bind import dialog
            const importOverlay = document.getElementById('import-overlay');
            document.getElementById('import-story-btn').addEventListener('click', () => {
                importOverlay.classList.add('open');
                Editor._loadImportable();
            });
            document.getElementById('import-close-btn').addEventListener('click', () => {
                importOverlay.classList.remove('open');
            });
            importOverlay.addEventListener('click', (e) => {
                if (e.target === importOverlay) importOverlay.classList.remove('open');
            });
            importOverlay.addEventListener('keydown', (e) => {
                if (e.key === 'Escape') importOverlay.classList.remove('open');
            });

            document.getElementById('import-manual-btn').addEventListener('click', () => {
                Editor._submitManualImport();
            });
            document.getElementById('import-manual-input').addEventListener('keydown', (e) => {
                if (e.key === 'Enter') { e.preventDefault(); Editor._submitManualImport(); }
            });

            // Bind "Regenerate All" overlay
            const regenOverlay = document.getElementById('regen-all-overlay');
            const regenLog = document.getElementById('regen-all-log');
            const regenBar = document.getElementById('regen-all-bar');
            const regenCounts = document.getElementById('regen-all-counts');
            const regenElapsed = document.getElementById('regen-all-elapsed');
            const regenProgressEl = document.getElementById('regen-all-progress');
            const regenStartBtn = document.getElementById('regen-all-start');
            const regenCancelBtn = document.getElementById('regen-all-cancel');
            const regenCloseBtn = document.getElementById('regen-all-close');
            const regenSkipPdfCb = document.getElementById('regen-all-skip-pdf');

            const appendRegenLog = (line, color = '') => {
                const stamp = new Date().toLocaleTimeString();
                const span = document.createElement('span');
                if (color) span.style.color = color;
                span.textContent = `${stamp}  ${line}\n`;
                regenLog.appendChild(span);
                regenLog.scrollTop = regenLog.scrollHeight;
            };

            document.getElementById('regen-all-btn').addEventListener('click', () => {
                regenOverlay.classList.add('open');
                regenLog.innerHTML = '';
                regenBar.style.width = '0%';
                regenCounts.textContent = '';
                regenElapsed.textContent = '';
                regenProgressEl.style.display = 'none';
                regenStartBtn.style.display = '';
                regenCancelBtn.style.display = 'none';
                regenStartBtn.disabled = false;
                Editor._regenAllRunId = null;
            });

            regenCloseBtn.addEventListener('click', () => {
                if (Editor._regenAllRunId && !Editor._regenAllCompleted) {
                    if (!confirm('A regenerate run is in progress. Closing this dialog leaves it running in the background. Continue?')) return;
                }
                regenOverlay.classList.remove('open');
            });
            regenOverlay.addEventListener('click', (e) => {
                if (e.target === regenOverlay) regenCloseBtn.click();
            });

            regenStartBtn.addEventListener('click', async () => {
                regenStartBtn.disabled = true;
                regenStartBtn.style.display = 'none';
                regenCancelBtn.style.display = '';
                regenProgressEl.style.display = '';
                appendRegenLog('Starting bulk regenerate…', 'var(--text-secondary)');
                Editor._regenAllCompleted = false;

                try {
                    const resp = await fetch('/api/editor/regenerate-all', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ skip_pdf: regenSkipPdfCb.checked }),
                    });
                    if (!resp.ok) {
                        const detail = await resp.json().catch(() => ({ detail: resp.statusText }));
                        throw new Error(JSON.stringify(detail.detail || detail));
                    }
                    const { run_id, total } = await resp.json();
                    Editor._regenAllRunId = run_id;
                    appendRegenLog(`Run ${run_id.slice(0, 8)} started — ${total} stories queued`);
                    Editor._streamRegenAll(run_id);
                } catch (err) {
                    appendRegenLog('ERROR: ' + err.message, 'var(--color-error,#e25)');
                    regenStartBtn.style.display = '';
                    regenStartBtn.disabled = false;
                    regenCancelBtn.style.display = 'none';
                }
            });

            regenCancelBtn.addEventListener('click', async () => {
                if (!Editor._regenAllRunId) return;
                appendRegenLog('Cancellation requested — current story will finish first…', 'var(--text-secondary)');
                try {
                    await fetch(`/api/editor/regenerate-all/cancel/${encodeURIComponent(Editor._regenAllRunId)}`, { method: 'POST' });
                } catch (err) {
                    appendRegenLog('Cancel failed: ' + err.message, 'var(--color-error,#e25)');
                }
            });

        } catch (err) {
            App._setContent(`<div class="empty-state"><h3>Error loading stories</h3><p>${err.message}</p></div>`);
        }
    },

    _streamRegenAll(runId) {
        const logEl = document.getElementById('regen-all-log');
        const barEl = document.getElementById('regen-all-bar');
        const countsEl = document.getElementById('regen-all-counts');
        const elapsedEl = document.getElementById('regen-all-elapsed');
        const startBtn = document.getElementById('regen-all-start');
        const cancelBtn = document.getElementById('regen-all-cancel');

        let total = 0, passed = 0, failed = 0, partial = 0, idx = 0;
        const t0 = Date.now();
        const tick = setInterval(() => {
            elapsedEl.textContent = `${Math.floor((Date.now() - t0) / 1000)}s`;
        }, 1000);

        const append = (line, color = '') => {
            const stamp = new Date().toLocaleTimeString();
            const span = document.createElement('span');
            if (color) span.style.color = color;
            span.textContent = `${stamp}  ${line}\n`;
            logEl.appendChild(span);
            logEl.scrollTop = logEl.scrollHeight;
        };

        const updateCounts = () => {
            countsEl.textContent = `${idx} of ${total}  ·  ${passed} ✓  ·  ${partial} ⚠  ·  ${failed} ✗`;
            if (total) barEl.style.width = `${Math.round((idx / total) * 100)}%`;
        };

        const es = new EventSource(`/api/editor/regenerate-all/stream/${encodeURIComponent(runId)}`);
        es.onmessage = (ev) => {
            let data; try { data = JSON.parse(ev.data); } catch { return; }
            if (data.type === 'suite_start') {
                total = data.total;
                append(`Suite started — ${total} stories, skip_pdf=${data.skip_pdf}`);
                updateCounts();
            } else if (data.type === 'story_start') {
                append(`▶ [${data.idx}/${data.total}] ${data.story}`);
            } else if (data.type === 'story_end') {
                idx = Math.max(idx, total ? Math.min(total, idx + 1) : idx + 1);
                if (data.status === 'passed') {
                    passed++;
                    append(`✓ ${data.story} — ${data.results_count} outputs (${(data.duration_ms / 1000).toFixed(1)}s)`, 'var(--color-success,#3a3)');
                } else if (data.status === 'partial') {
                    partial++;
                    append(`⚠ ${data.story} — ${data.results_count} outputs, ${data.errors.length} errors (${(data.duration_ms / 1000).toFixed(1)}s)`, 'var(--warning,#c83)');
                    for (const e of (data.errors || [])) append(`    · ${e}`, 'var(--warning,#c83)');
                } else {
                    failed++;
                    append(`✗ ${data.story} — failed`, 'var(--color-error,#e25)');
                    for (const e of (data.errors || [])) append(`    · ${e}`, 'var(--color-error,#e25)');
                }
                updateCounts();
            } else if (data.type === 'cancelled') {
                append(`Cancelled at story ${data.at_index + 1}`, 'var(--text-secondary)');
            } else if (data.type === 'suite_complete') {
                const s = data.summary || {};
                append(`Suite complete — ${s.passed} passed, ${s.failed} failed (${((s.duration_ms || 0) / 1000).toFixed(1)}s total)`,
                    s.failed ? 'var(--color-error,#e25)' : 'var(--color-success,#3a3)');
                Editor._regenAllCompleted = true;
                clearInterval(tick);
                startBtn.style.display = '';
                startBtn.disabled = false;
                cancelBtn.style.display = 'none';
                es.close();
            }
        };
        es.onerror = () => {
            // Browsers spam onerror on normal close; only log if we haven't seen suite_complete
            if (!Editor._regenAllCompleted) {
                append('Stream interrupted — refresh the page to reattach if the run is still going', 'var(--warning,#c83)');
            }
            clearInterval(tick);
            es.close();
        };
    },

    async _submitCreateStory() {
        const errEl = document.getElementById('create-story-error');
        const name = (document.getElementById('create-story-name').value || '').trim();
        const title = (document.getElementById('create-story-title').value || '').trim();
        const author = (document.getElementById('create-story-author').value || '').trim();
        const genre = document.getElementById('create-story-genre').value || '';
        const chapters = parseInt(document.getElementById('create-story-chapters').value, 10) || 1;
        const rating = document.getElementById('create-story-rating').value || 'explicit';

        if (!title) {
            errEl.textContent = 'Title is required.';
            errEl.style.display = 'block';
            return;
        }
        if (!name) {
            errEl.textContent = 'Folder name is required.';
            errEl.style.display = 'block';
            return;
        }
        if (!/^[A-Za-z0-9_]+$/.test(name)) {
            errEl.textContent = 'Folder name may only contain letters, digits, and underscores.';
            errEl.style.display = 'block';
            return;
        }

        const submitBtn = document.getElementById('create-story-submit');
        submitBtn.disabled = true;
        submitBtn.textContent = 'Creating...';
        errEl.style.display = 'none';

        // Read optional file content
        let file_content = '';
        let file_format = '';
        const fileInput = document.getElementById('create-story-file');
        if (fileInput && fileInput.files.length > 0) {
            const file = fileInput.files[0];
            const ext = file.name.split('.').pop().toLowerCase();
            file_format = ext;
            try {
                file_content = await file.text();
            } catch (e) {
                errEl.textContent = 'Could not read file: ' + e.message;
                errEl.style.display = 'block';
                submitBtn.disabled = false;
                submitBtn.textContent = 'Create';
                return;
            }
        }

        try {
            const resp = await fetch('/api/editor/stories/create', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({ name, title, author, genre, chapters, rating, file_content, file_format }),
            });
            const data = await resp.json();
            if (!resp.ok) {
                throw new Error(data.detail || 'Failed to create story');
            }
            document.getElementById('create-story-overlay').classList.remove('open');
            location.hash = '#/editor/' + data.story_name;
        } catch (err) {
            errEl.textContent = err.message;
            errEl.style.display = 'block';
        } finally {
            submitBtn.disabled = false;
            submitBtn.textContent = 'Create';
        }
    },

    // ---------------------------------------------------------------------------
    // Import from platform
    // ---------------------------------------------------------------------------

    async _loadImportable() {
        const container = document.getElementById('import-content');
        container.innerHTML = '<div class="loading-spinner">Loading available submissions...</div>';

        try {
            const resp = await fetch('/api/editor/import/available');
            const data = await resp.json();
            if (!resp.ok) throw new Error(data.detail || 'Failed to load');

            const subs = data.submissions || [];
            const comingSoon = data.coming_soon || [];

            if (subs.length === 0 && comingSoon.length === 0) {
                container.innerHTML = '<p class="import-empty">No importable submissions found. Make sure your platforms have been polled at least once.</p>';
                return;
            }

            // Group by platform
            const grouped = {};
            for (const s of subs) {
                if (!grouped[s.platform]) grouped[s.platform] = { label: s.platform_label, items: [] };
                grouped[s.platform].items.push(s);
            }

            // Platform icons (reuse existing platform badge classes if available)
            const platformIcons = {
                ib: 'IB', sf: 'SF', fa: 'FA', sqw: 'SQW', ao3: 'AO3',
            };

            let html = '';

            for (const [plat, group] of Object.entries(grouped)) {
                html += `<div class="import-platform-group">`;
                html += `<h4 class="import-platform-header"><span class="import-platform-badge import-badge-${plat}">${platformIcons[plat] || plat.toUpperCase()}</span> ${Utils.escapeHtml(group.label)} <span class="import-count">(${group.items.length})</span></h4>`;
                html += `<div class="import-list">`;
                for (const s of group.items) {
                    const ratingBadge = s.rating ? `<span class="import-rating">${Utils.escapeHtml(s.rating)}</span>` : '';
                    html += `
                        <div class="import-row" id="import-row-${plat}-${s.submission_id}">
                            <div class="import-row-info">
                                <span class="import-title">${Utils.escapeHtml(s.title || 'Untitled')}</span>
                                ${ratingBadge}
                                <span class="import-author">by ${Utils.escapeHtml(s.author || 'unknown')}</span>
                            </div>
                            <button class="btn btn-sm import-btn" data-platform="${plat}" data-id="${s.submission_id}" data-title="${Utils.escapeHtml(s.title)}">Import</button>
                        </div>`;
                }
                html += `</div></div>`;
            }

            // Coming soon platforms
            if (comingSoon.length > 0) {
                html += `<div class="import-platform-group import-coming-soon">`;
                html += `<h4 class="import-platform-header" style="opacity:0.5">Coming soon</h4>`;
                for (const cs of comingSoon) {
                    html += `<p class="import-coming-soon-label"><span class="import-platform-badge">${Utils.escapeHtml(cs.label)}</span> Import not yet available</p>`;
                }
                html += `</div>`;
            }

            container.innerHTML = html;

            // Bind import buttons
            container.querySelectorAll('.import-btn').forEach(btn => {
                btn.addEventListener('click', () => {
                    const platform = btn.dataset.platform;
                    const id = btn.dataset.id;
                    Editor._doImport(platform, id, btn);
                });
            });

        } catch (err) {
            container.innerHTML = `<p class="import-error">Failed to load: ${Utils.escapeHtml(err.message)}</p>`;
        }
    },

    /* Parse "https://archiveofourown.org/works/12345" / "ib:12345" /
     * "12345" + selected/inferred platform into {platform, id}.
     * Returns null if the input doesn't match a known shape. */
    _parseImportRef(raw) {
        const s = (raw || '').trim();
        if (!s) return null;
        // Explicit platform prefix: ib:12345, sf:12345, fa:12345, ao3:12345, sqw:12345
        const prefixed = s.match(/^(ib|sf|fa|ao3|sqw)\s*[:\/\s]+(\d+)/i);
        if (prefixed) return { platform: prefixed[1].toLowerCase(), id: prefixed[2] };
        // URL forms
        const urlPatterns = [
            { re: /inkbunny\.net\/s\/(\d+)/i, plat: 'ib' },
            { re: /sofurry\.com\/(?:view|s)\/(\d+)/i, plat: 'sf' },
            { re: /furaffinity\.net\/view\/(\d+)/i, plat: 'fa' },
            { re: /archiveofourown\.org\/works\/(\d+)/i, plat: 'ao3' },
            { re: /squidgeworld\.org\/works\/(\d+)/i, plat: 'sqw' },
        ];
        for (const { re, plat } of urlPatterns) {
            const m = s.match(re);
            if (m) return { platform: plat, id: m[1] };
        }
        return null;
    },

    async _submitManualImport() {
        const input = document.getElementById('import-manual-input');
        const status = document.getElementById('import-manual-status');
        const btn = document.getElementById('import-manual-btn');
        const ref = Editor._parseImportRef(input.value);
        if (!ref) {
            status.textContent = 'Could not parse — try a URL like https://archiveofourown.org/works/12345 or "ao3:12345".';
            status.className = 'import-manual-status error';
            return;
        }
        status.textContent = `Importing ${ref.platform.toUpperCase()} ${ref.id}…`;
        status.className = 'import-manual-status pending';
        btn.disabled = true;
        try {
            const resp = await fetch(`/api/editor/import/${ref.platform}/${ref.id}`, { method: 'POST' });
            const data = await resp.json();
            if (!resp.ok) throw new Error(data.detail || 'Import failed');
            const draftLabel = data.is_draft ? ' (draft)' : '';
            status.innerHTML = `Imported${draftLabel}: <a href="#/editor/${data.story_name}">${Utils.escapeHtml(data.title || data.story_name)}</a>`;
            status.className = 'import-manual-status success';
            input.value = '';
        } catch (err) {
            status.textContent = `Import failed: ${err.message}`;
            status.className = 'import-manual-status error';
        } finally {
            btn.disabled = false;
        }
    },

    async _doImport(platform, submissionId, btn) {
        const row = document.getElementById(`import-row-${platform}-${submissionId}`);
        btn.disabled = true;
        btn.textContent = 'Importing...';
        btn.classList.add('importing');

        try {
            const resp = await fetch(`/api/editor/import/${platform}/${submissionId}`, {
                method: 'POST',
            });
            const data = await resp.json();
            if (!resp.ok) throw new Error(data.detail || 'Import failed');

            // Success — mark the row and offer navigation
            btn.textContent = data.is_draft ? 'Done (draft)' : 'Done';
            btn.classList.remove('importing');
            btn.classList.add('import-done');

            if (row) {
                row.classList.add('import-row-success');
                if (data.is_draft) row.classList.add('import-row-draft');
                const link = document.createElement('a');
                link.href = `#/editor/${data.story_name}`;
                link.className = 'btn btn-sm import-open-btn';
                link.textContent = 'Open';
                link.addEventListener('click', () => {
                    document.getElementById('import-overlay').classList.remove('open');
                });
                btn.replaceWith(link);
            }

        } catch (err) {
            btn.disabled = false;
            btn.textContent = 'Retry';
            btn.classList.remove('importing');
            btn.classList.add('import-error-btn');

            // Show error message in row
            if (row) {
                let errEl = row.querySelector('.import-row-error');
                if (!errEl) {
                    errEl = document.createElement('div');
                    errEl.className = 'import-row-error';
                    row.appendChild(errEl);
                }
                errEl.textContent = err.message;
            }
        }
    },

    // ---------------------------------------------------------------------------
    // Editor page
    // ---------------------------------------------------------------------------

    async renderEditor(storyName) {
        // Clean up previous editor state
        clearInterval(this.autoSaveTimer);
        if (this._beforeUnloadHandler) {
            window.removeEventListener('beforeunload', this._beforeUnloadHandler);
        }
        if (this.cmView) { this.cmView.destroy(); this.cmView = null; }
        if (this.cmSourceView) { this.cmSourceView.destroy(); this.cmSourceView = null; }
        if (this.cmCssView) { this.cmCssView.destroy(); this.cmCssView = null; }
        this._wysiwygEditSource = null;
        clearTimeout(this._wysiwygSyncTimer);

        this.storyName = storyName;
        this.isDirty = false;

        App._setContent(`
            <div class="editor-container">
                <div class="editor-toolbar" id="editor-toolbar">
                    <a href="#/editor" class="editor-back">← Stories</a>
                    <span class="editor-title" id="editor-title">${Utils.escapeHtml(storyName.replace(/_/g, ' '))}</span>
                    <div class="editor-actions">
                        <!-- Secondary cluster — collapsed behind the
                             ⋯ More button on mobile so the toolbar
                             stays one row. Save + Metadata stay
                             visible as primary affordances. -->
                        <div class="editor-actions-secondary" id="editor-actions-secondary">
                            <select id="editor-chapter-nav" title="Jump to chapter"></select>
                            <span id="editor-slop" class="editor-slop" title="Slop score"></span>
                            <span id="editor-status" class="editor-status"></span>
                            <span id="editor-wordcount" class="editor-wordcount"></span>
                            <button id="editor-css-btn" class="btn btn-sm btn-outline">CSS</button>
                            <div class="regen-dropdown" id="regen-dropdown">
                                <button id="editor-regen-btn" class="btn btn-sm btn-outline">Regenerate &#9662;</button>
                                <div class="regen-dropdown-menu" id="regen-dropdown-menu">
                                    <button data-regen="all">All formats</button>
                                    <button data-regen="html">HTML only (SF/AO3/SQW)</button>
                                    <button data-regen="bbcode">BBCode only (IB/WS)</button>
                                    <button data-regen="styled">Styled HTML + CSS</button>
                                    <button data-regen="sqw">SquidgeWorld only</button>
                                    <button data-regen="pdf">PDF only</button>
                                    <button data-regen="epub">EPUB only</button>
                                    <button data-regen="chapters">Chapter splits only</button>
                                </div>
                            </div>
                            <div class="regen-dropdown" id="downloads-dropdown">
                                <button id="editor-downloads-btn" class="btn btn-sm btn-outline" title="Download a generated format to this device">Downloads &#9662;</button>
                                <div class="regen-dropdown-menu" id="downloads-dropdown-menu">
                                    <div class="downloads-loading" style="padding:0.4em 0.75em;color:#888">Loading…</div>
                                </div>
                            </div>
                            <button id="editor-publish-btn" class="btn btn-sm btn-outline" title="Check publishability across all platforms">Publish</button>
                            <button id="editor-share-btn" class="btn btn-sm btn-outline" title="Create a read-only public link to share this draft with a beta reader">&#128279; Share draft</button>
                            <button id="editor-format-btn" class="btn btn-sm btn-outline" title="Format source code (Shift+Alt+F)">Format</button>
                            <div class="format-tabs" id="format-tabs">
                                <button class="format-tab active" data-fmt="clean_html">Clean HTML</button>
                                <button class="format-tab" data-fmt="sofurry_html">SoFurry</button>
                                <button class="format-tab" data-fmt="bbcode">BBCode</button>
                                <button class="format-tab" data-fmt="styled_html">Styled</button>
                            </div>
                        </div>
                        <!-- ⋯ button — hidden on desktop, only mobile -->
                        <button id="editor-more-btn" class="btn btn-sm btn-outline editor-more-btn" type="button" title="More actions">&hellip;</button>
                        <button id="editor-save-btn" class="btn btn-sm">Save</button>
                        <button id="editor-metadata-btn" class="btn btn-sm btn-outline">Metadata</button>
                    </div>
                </div>
                <!-- Mobile-only single-panel switcher. CSS hides this on
                     desktop. Each tab maps to one of the 4 quad panels;
                     a 5th tab is appended dynamically when the CSS theme
                     editor is opened. -->
                <div class="editor-mobile-tabs" id="editor-mobile-tabs" role="tablist">
                    <button class="editor-mobile-tab active" data-panel="panel-md-code" type="button">Edit</button>
                    <button class="editor-mobile-tab" data-panel="panel-md-preview" type="button">Rich</button>
                    <button class="editor-mobile-tab" data-panel="panel-fmt-source" type="button">Format</button>
                    <button class="editor-mobile-tab" data-panel="panel-fmt-preview" type="button">Preview</button>
                </div>
                <div class="editor-quad" id="editor-quad">
                    <div class="editor-quad-panel" id="panel-md-code">
                        <div class="preview-panel-header"><button class="panel-toggle" data-panel="panel-md-code" title="Hide panel">&#128065;</button> Markdown Source</div>
                        <div id="editor-cm-container" class="editor-cm-container"></div>
                    </div>
                    <div class="editor-quad-panel" id="panel-md-preview">
                        <div class="preview-panel-header"><button class="panel-toggle" data-panel="panel-md-preview" title="Hide panel">&#128065;</button> Rich Editor</div>
                        <div class="wysiwyg-toolbar" id="wysiwyg-toolbar">
                            <button data-cmd="undo" title="Undo (Ctrl+Z)">&#8630;</button>
                            <button data-cmd="redo" title="Redo (Ctrl+Y)">&#8631;</button>
                            <span class="toolbar-sep"></span>
                            <button data-cmd="bold" title="Bold (Ctrl+B)"><strong>B</strong></button>
                            <button data-cmd="italic" title="Italic (Ctrl+I)"><em>I</em></button>
                            <span class="toolbar-sep"></span>
                            <button data-cmd="heading" title="Chapter Heading">H1</button>
                            <button data-cmd="hr" title="Section Break">&#8213;</button>
                            <span class="toolbar-sep"></span>
                            <button data-anchor="title">T</button>
                            <button data-anchor="subtitle">Sub</button>
                            <button data-anchor="byline">By</button>
                            <span class="toolbar-sep"></span>
                            <button data-anchor="warning">&#9888;</button>
                            <button data-anchor="disclaimer">Disc</button>
                            <button data-anchor="fanfiction">FF</button>
                            <span class="toolbar-sep"></span>
                            <button data-anchor="body">Body</button>
                            <span class="toolbar-sep"></span>
                            <button data-anchor="text-sent">&#8594; Sent</button>
                            <button data-anchor="text-received">&#8592; Recv</button>
                            <button data-anchor="phone-incoming">&#9742; Phone</button>
                        </div>
                        <div class="preview-panel-body preview-html" id="editor-preview-rendered-body" contenteditable="true" spellcheck="true">
                            <p style="color:var(--text-secondary)">Loading...</p>
                        </div>
                    </div>
                    <div class="editor-quad-panel" id="panel-fmt-source">
                        <div class="preview-panel-header"><button class="panel-toggle" data-panel="panel-fmt-source" title="Hide panel">&#128065;</button> <span id="editor-source-header">Format Source</span></div>
                        <div class="preview-panel-body" id="editor-preview-source-body">
                            <p style="color:var(--text-secondary)">Loading...</p>
                        </div>
                    </div>
                    <div class="editor-quad-panel" id="panel-fmt-preview">
                        <div class="preview-panel-header"><button class="panel-toggle" data-panel="panel-fmt-preview" title="Hide panel">&#128065;</button> <span id="editor-fmt-preview-header">Format Preview</span></div>
                        <div class="preview-panel-body" id="editor-preview-fmt-body">
                            <p style="color:var(--text-secondary)">Loading...</p>
                        </div>
                    </div>
                </div>
            </div>
        `);

        // Load content
        try {
            const resp = await fetch(`/api/editor/stories/${encodeURIComponent(storyName)}/content`);
            if (!resp.ok) throw new Error(await resp.text());
            const data = await resp.json();

            this.lastSavedContent = data.content;
            this.lastMtime = data.last_modified;
            this.chapters = data.chapters || [];
            this._updateWordCount(data.word_count);
            this._updateStatus('Loaded');

            // Check for crash recovery draft in localStorage
            const recoveryKey = `editor_recovery_${storyName}`;
            const recovered = localStorage.getItem(recoveryKey);
            let initialContent = data.content;
            if (recovered && recovered !== data.content) {
                const useRecovery = confirm('A recovery draft was found (unsaved changes from a previous session). Restore it?');
                if (useRecovery) {
                    initialContent = recovered;
                    this.isDirty = true;
                    this._updateStatus('Recovered from auto-save');
                } else {
                    localStorage.removeItem(recoveryKey);
                }
            }

            // Initialize CodeMirror
            this._initCodeMirror(initialContent);

            // Bind toolbar events
            document.querySelectorAll('#format-tabs .format-tab').forEach(btn => {
                btn.addEventListener('click', () => {
                    document.querySelectorAll('#format-tabs .format-tab').forEach(b => b.classList.remove('active'));
                    btn.classList.add('active');
                    this.switchFormat(btn.dataset.fmt);
                    // On mobile, picking a format implies the user wants
                    // to look at the result — auto-switch to the format
                    // panel (unless they're already there or on Preview).
                    if (App.isMobileLayoutActive && App.isMobileLayoutActive()) {
                        const cur = document.querySelector('.editor-quad-panel.mobile-active');
                        const onFmt = cur && (cur.id === 'panel-fmt-source' || cur.id === 'panel-fmt-preview');
                        if (!onFmt) this.setMobileActivePanel('panel-fmt-source');
                    }
                });
            });

            // Mobile single-panel tab switcher. Initialised to the
            // Edit (Markdown source) panel — the most common starting
            // point. All four panels remain in the DOM; CSS hides every
            // panel except the one with .mobile-active.
            document.querySelectorAll('#editor-mobile-tabs .editor-mobile-tab').forEach(btn => {
                btn.addEventListener('click', () => {
                    this.setMobileActivePanel(btn.dataset.panel);
                });
            });
            this.setMobileActivePanel('panel-md-code');

            // Mobile More-actions toggle. Toolbar starts collapsed;
            // tapping ⋯ slides the secondary actions down as a wrap
            // row. Re-tap to collapse. Outside-click closes too.
            document.getElementById('editor-more-btn')?.addEventListener('click', (e) => {
                e.stopPropagation();
                document.getElementById('editor-toolbar')?.classList.toggle('actions-open');
            });
            document.addEventListener('click', (e) => {
                const toolbar = document.getElementById('editor-toolbar');
                if (!toolbar || !toolbar.classList.contains('actions-open')) return;
                if (e.target.closest('#editor-actions-secondary')) return;
                if (e.target.closest('#editor-more-btn')) return;
                toolbar.classList.remove('actions-open');
            });
            document.getElementById('editor-css-btn')?.addEventListener('click', () => this.toggleCssEditor());
            document.getElementById('editor-metadata-btn')?.addEventListener('click', () => MetaEditor.toggle());
            document.getElementById('editor-save-btn')?.addEventListener('click', () => this.save());
            // Regen dropdown toggle + menu items
            document.getElementById('editor-regen-btn')?.addEventListener('click', (e) => {
                e.stopPropagation();
                document.getElementById('regen-dropdown-menu')?.classList.toggle('open');
            });
            document.querySelectorAll('#regen-dropdown-menu button[data-regen]').forEach(btn => {
                btn.addEventListener('click', (e) => {
                    e.stopPropagation();
                    document.getElementById('regen-dropdown-menu')?.classList.remove('open');
                    this.regenerate(btn.dataset.regen === 'all' ? null : [btn.dataset.regen]);
                });
            });
            // Downloads dropdown — lazy-fetch the format list the first
            // time it opens, then keep the rendered menu around for
            // subsequent opens. Rebuilt on every regenerate() so freshly
            // produced files show up without a page reload.
            document.getElementById('editor-downloads-btn')?.addEventListener('click', (e) => {
                e.stopPropagation();
                const menu = document.getElementById('downloads-dropdown-menu');
                if (!menu) return;
                const willOpen = !menu.classList.contains('open');
                document.getElementById('regen-dropdown-menu')?.classList.remove('open');
                menu.classList.toggle('open');
                if (willOpen && !menu.dataset.populated) {
                    this._populateDownloadsMenu();
                }
            });
            // Close dropdowns on outside click
            document.addEventListener('click', () => {
                document.getElementById('regen-dropdown-menu')?.classList.remove('open');
                document.getElementById('downloads-dropdown-menu')?.classList.remove('open');
            });
            document.getElementById('editor-publish-btn')?.addEventListener('click', () => PublishCheck.open(storyName));
            document.getElementById('editor-share-btn')?.addEventListener('click', () => this._openShareDraft(storyName));
            document.getElementById('editor-format-btn')?.addEventListener('click', () => this.formatSource());
            document.getElementById('editor-chapter-nav')?.addEventListener('change', (e) => this._jumpToChapter(parseInt(e.target.value)));
            document.querySelectorAll('.panel-toggle').forEach(btn => {
                btn.addEventListener('click', () => this.togglePanel(btn.dataset.panel));
            });

            // Initialize WYSIWYG
            this._initTurndown();
            this._initWysiwygToolbar();
            this._initWysiwygInput();

            // Cache front matter from initial content
            this._cacheFrontMatter(initialContent);

            // Beforeunload warning (single handler, cleaned up on re-render)
            this._beforeUnloadHandler = (e) => {
                if (this.isDirty) { e.preventDefault(); e.returnValue = ''; }
            };
            window.addEventListener('beforeunload', this._beforeUnloadHandler);

            // Auto-save to localStorage every 30s
            this.autoSaveTimer = setInterval(() => {
                if (this.isDirty && this.cmView) {
                    localStorage.setItem(recoveryKey, this.cmView.state.doc.toString());
                }
            }, 30000);

            // Build chapter nav + initial preview
            this._updateChapterNav();
            this._requestPreview();
            this._requestSlopScore();

        } catch (err) {
            const container = document.getElementById('editor-cm-container');
            if (container) container.innerHTML = `<p style="color:var(--color-error);padding:20px">Error loading: ${err.message}</p>`;
        }
    },

    // ---------------------------------------------------------------------------
    // CodeMirror initialization
    // ---------------------------------------------------------------------------

    _initCodeMirror(content) {
        const container = document.getElementById('editor-cm-container');
        if (!container || typeof CM === 'undefined') {
            // Fallback to textarea if CM bundle didn't load
            container.innerHTML = '<textarea id="editor-textarea" spellcheck="true"></textarea>';
            const ta = container.querySelector('textarea');
            ta.value = content;
            ta.addEventListener('input', () => this._onInput());
            return;
        }

        // Custom anchor highlighting
        const anchorHighlight = CM.ViewPlugin.fromClass(class {
            constructor(view) { this.decorations = this.buildDecos(view); }
            update(update) { if (update.docChanged || update.viewportChanged) this.decorations = this.buildDecos(update.view); }
            buildDecos(view) {
                const builder = new CM.Decoration.none.constructor();
                // Can't easily build decorations without RangeSetBuilder — skip for now
                return CM.Decoration.none;
            }
        }, { decorations: v => v.decorations });

        const darkTheme = CM.EditorView.theme({
            '&': { height: '100%', fontSize: '13px' },
            '.cm-scroller': { overflow: 'auto', fontFamily: "'Consolas', 'Monaco', 'Courier New', monospace" },
            '.cm-content': { padding: '10px 0' },
            '.cm-line': { padding: '0 12px' },
            '.cm-gutters': { background: 'var(--surface-elevated)', color: 'var(--text-tertiary)', border: 'none', minWidth: '3em' },
            '.cm-activeLineGutter': { background: 'var(--surface-primary)' },
            '.cm-activeLine': { background: 'rgba(255,255,255,0.03)' },
        });

        // Ctrl+S keybinding
        const saveKeymap = CM.keymap.of([
            { key: 'Mod-s', run: () => { this.save(); return true; } },
            { key: 'Shift-Alt-f', run: () => { this.formatSource(); return true; } },
        ]);

        this.cmView = new CM.EditorView({
            doc: content,
            extensions: [
                CM.basicSetup,
                CM.markdown(),
                CM.oneDark,
                darkTheme,
                saveKeymap,
                CM.lineNumbers(),
                CM.highlightActiveLine(),
                CM.highlightActiveLineGutter(),
                CM.EditorView.lineWrapping,
                CM.EditorView.updateListener.of(update => {
                    if (update.docChanged) this._onInput();
                }),
                // Scroll sync: editor → other panels
                CM.EditorView.domEventHandlers({
                    scroll: () => { this._syncScroll('cm-editor'); },
                    mouseup: () => { this._syncSelectionFromCM(); },
                }),
            ],
            parent: container,
        });

        // Scroll sync: preview panels → other panels
        for (const id of ['editor-preview-rendered-body', 'editor-preview-source-body', 'editor-preview-fmt-body']) {
            const el = document.getElementById(id);
            if (el) el.addEventListener('scroll', () => this._syncScroll(id));
        }

        // Selection sync: selecting text in any panel highlights it in the others
        for (const id of ['editor-preview-rendered-body', 'editor-preview-fmt-body']) {
            const el = document.getElementById(id);
            if (el) el.addEventListener('mouseup', () => this._syncSelection(id));
        }
    },

    _syncSelectionFromCM() {
        this._clearSelectionHighlights();
        if (!this.cmView) return;
        const { from, to } = this.cmView.state.selection.main;
        if (from === to) return;
        const text = this.cmView.state.sliceDoc(from, to).trim();
        if (text.length < 3 || text.length > 500) return;

        // Strip markdown formatting to get the plain text for HTML panel search
        const plain = text.replace(/\*+/g, '').replace(/_+/g, '').trim();
        if (!plain) return;

        // Highlight in format preview only (skip contenteditable panel 2 to avoid DOM corruption)
        const fmtPreview = document.getElementById('editor-preview-fmt-body');
        if (fmtPreview) this._highlightInHtml(fmtPreview, plain);
        // Highlight in CM source view
        if (this.cmSourceView) this._highlightInCM(this.cmSourceView, plain);
    },

    _syncSelectionFromCMSource() {
        this._clearSelectionHighlights();
        if (!this.cmSourceView) return;
        const { from, to } = this.cmSourceView.state.selection.main;
        if (from === to) return;
        const text = this.cmSourceView.state.sliceDoc(from, to).trim();
        if (text.length < 3 || text.length > 500) return;

        // Strip HTML tags to get plain text
        const plain = text.replace(/<[^>]+>/g, '').replace(/&[a-z]+;/gi, ' ').trim();
        if (!plain) return;

        // Highlight in CM editor and format preview (skip contenteditable panel 2)
        if (this.cmView) this._highlightInCM(this.cmView, plain);
        const fmtPreview = document.getElementById('editor-preview-fmt-body');
        if (fmtPreview) this._highlightInHtml(fmtPreview, plain);
    },

    _selectionHighlights: [],  // track active highlights for cleanup

    _syncSelection(sourceId) {
        const sel = window.getSelection();
        const text = sel?.toString().trim();

        // Clear previous highlights
        this._clearSelectionHighlights();

        if (!text || text.length < 3 || text.length > 500) return;

        // Strip HTML tags to get plain text for searching in source
        const searchText = text;

        // Highlight in CM editor (panel 1)
        if (this.cmView) this._highlightInCM(this.cmView, searchText);

        // Highlight in CM source view (panel 3)
        if (this.cmSourceView) this._highlightInCM(this.cmSourceView, searchText);

        // Highlight in format preview only (skip contenteditable panel 2)
        if (sourceId !== 'editor-preview-fmt-body') {
            const fmtPreview = document.getElementById('editor-preview-fmt-body');
            if (fmtPreview) this._highlightInHtml(fmtPreview, searchText);
        }
    },

    _highlightInCM(view, text) {
        // Find the text in the CM document and scroll to + select it
        const doc = view.state.doc.toString();
        const idx = doc.indexOf(text);
        if (idx === -1) {
            // Try stripped version (markdown has * for italic, HTML has tags)
            const stripped = text.replace(/[*_]/g, '');
            const idx2 = doc.replace(/[*_]/g, '').indexOf(stripped);
            if (idx2 === -1) return;
            // Map stripped position back to original — approximate by using same offset
            view.dispatch({
                selection: { anchor: idx2, head: idx2 + text.length },
                effects: CM.EditorView.scrollIntoView(idx2, { y: 'center' }),
            });
            return;
        }
        view.dispatch({
            selection: { anchor: idx, head: idx + text.length },
            effects: CM.EditorView.scrollIntoView(idx, { y: 'center' }),
        });
    },

    _highlightInHtml(container, text) {
        // Walk text nodes and wrap first match in a <mark>
        const walker = document.createTreeWalker(container, NodeFilter.SHOW_TEXT);
        let accumulated = '';
        const nodes = [];

        while (walker.nextNode()) {
            nodes.push({ node: walker.currentNode, start: accumulated.length });
            accumulated += walker.currentNode.textContent;
        }

        const matchIdx = accumulated.indexOf(text);
        if (matchIdx === -1) return;

        // Find which text node(s) contain the match
        for (const { node, start } of nodes) {
            const nodeEnd = start + node.textContent.length;
            if (nodeEnd <= matchIdx) continue;
            if (start >= matchIdx + text.length) break;

            const localStart = Math.max(0, matchIdx - start);
            const localEnd = Math.min(node.textContent.length, matchIdx + text.length - start);

            const range = document.createRange();
            range.setStart(node, localStart);
            range.setEnd(node, localEnd);

            const mark = document.createElement('mark');
            mark.className = 'selection-sync-highlight';
            mark.style.cssText = 'background: rgba(255, 200, 50, 0.4); border-radius: 2px;';
            range.surroundContents(mark);
            this._selectionHighlights.push(mark);

            // Scroll the first highlight into view
            if (this._selectionHighlights.length === 1) {
                mark.scrollIntoView({ block: 'center', behavior: 'smooth' });
            }
        }
    },

    _clearSelectionHighlights() {
        for (const mark of this._selectionHighlights) {
            const parent = mark.parentNode;
            if (parent) {
                parent.replaceChild(document.createTextNode(mark.textContent), mark);
                parent.normalize();  // merge adjacent text nodes
            }
        }
        this._selectionHighlights = [];
    },

    _syncScroll(sourceId) {
        if (this._syncingScroll || this._wysiwygEditSource) return;
        this._syncingScroll = true;
        clearTimeout(this._scrollLockTimer);
        try {
            // Get scroll percentage from whichever panel triggered the scroll
            let pct = 0;
            const _pct = (el) => el.scrollTop / (el.scrollHeight - el.clientHeight || 1);

            if (sourceId === 'cm-editor') {
                const s = this.cmView?.dom.querySelector('.cm-scroller');
                if (s) pct = _pct(s);
            } else if (sourceId === 'cm-source') {
                const s = this.cmSourceView?.dom.querySelector('.cm-scroller');
                if (s) pct = _pct(s);
            } else {
                const el = document.getElementById(sourceId);
                if (el) pct = _pct(el);
            }

            const _apply = (el) => { el.scrollTop = pct * (el.scrollHeight - el.clientHeight); };

            // Sync to CM editor (panel 1)
            if (sourceId !== 'cm-editor') {
                const s = this.cmView?.dom.querySelector('.cm-scroller');
                if (s) _apply(s);
            }
            // Sync to CM source view (panel 3)
            if (sourceId !== 'cm-source' && this.cmSourceView) {
                const s = this.cmSourceView.dom.querySelector('.cm-scroller');
                if (s) _apply(s);
            }
            // Sync to HTML preview panels (panel 2 + panel 4)
            for (const id of ['editor-preview-rendered-body', 'editor-preview-fmt-body']) {
                if (id === sourceId) continue;
                const el = document.getElementById(id);
                if (el) _apply(el);
            }
        } finally {
            // Keep the lock active for 60ms so cascading scroll events
            // (fired async by the browser after setting scrollTop) are ignored
            this._scrollLockTimer = setTimeout(() => { this._syncingScroll = false; }, 60);
        }
    },

    /** BBCode language definition for CodeMirror */
    _bbcodeLang: null,
    _getBBCodeLang() {
        if (this._bbcodeLang) return this._bbcodeLang;
        if (typeof CM === 'undefined' || !CM.StreamLanguage) return null;

        this._bbcodeLang = CM.StreamLanguage.define({
            token(stream) {
                // Opening tags: [b], [i], [center], [t], [color=#hex], [size=N], [right], [left]
                if (stream.match(/^\[\/?(b|i|u|s|center|right|left|t|url|img|quote)\]/i)) {
                    return 'keyword';
                }
                // Tags with attributes: [color=#hex], [size=N], [url=...]
                if (stream.match(/^\[\/?(?:color|size|url|font)=[^\]]*\]/i)) {
                    return 'keyword';
                }
                // Closing tags catch-all
                if (stream.match(/^\[\/[a-z]+\]/i)) {
                    return 'keyword';
                }
                // Unicode decorative chars (section breaks, separators)
                if (stream.match(/^[─✦✧⚜★☆·⸰✹❀☽☾◆⚝✿❋⁕✶📱❤♥⟨⟩]+/)) {
                    return 'atom';
                }
                // Advance one char
                stream.next();
                return null;
            },
        });
        return this._bbcodeLang;
    },

    /** Create a CodeMirror instance for viewing/editing non-MD content */
    _createCmInstance(container, content, lang, readOnly = false) {
        if (typeof CM === 'undefined') return null;
        container.innerHTML = '';
        const extensions = [
            CM.oneDark,
            CM.EditorView.theme({
                '&': { height: '100%', fontSize: '12px' },
                '.cm-scroller': { overflow: 'auto', fontFamily: "'Consolas', 'Monaco', monospace" },
                '.cm-gutters': { background: 'var(--surface-elevated)', color: 'var(--text-tertiary)', border: 'none' },
            }),
            CM.lineNumbers(),
            CM.EditorView.lineWrapping,
        ];
        if (lang === 'html') extensions.push(CM.html());
        else if (lang === 'css') extensions.push(CM.css());
        else if (lang === 'bbcode') {
            const bbLang = this._getBBCodeLang();
            if (bbLang) extensions.push(bbLang);
        }
        if (readOnly) extensions.push(CM.EditorState.readOnly.of(true));
        else extensions.push(CM.basicSetup);

        return new CM.EditorView({ doc: content, extensions, parent: container });
    },

    /** Update a CM instance's content without recreating it */
    _updateCmContent(view, content) {
        if (!view) return;
        view.dispatch({ changes: { from: 0, to: view.state.doc.length, insert: content } });
    },

    /** Get the current editor content (works with both CM and textarea fallback) */
    _getContent() {
        if (this.cmView) return this.cmView.state.doc.toString();
        const ta = document.getElementById('editor-textarea');
        return ta ? ta.value : '';
    },

    /** Set the editor content */
    _setEditorContent(text) {
        if (this.cmView) {
            this.cmView.dispatch({
                changes: { from: 0, to: this.cmView.state.doc.length, insert: text },
            });
        } else {
            const ta = document.getElementById('editor-textarea');
            if (ta) ta.value = text;
        }
    },

    // ---------------------------------------------------------------------------
    // Chapter navigation
    // ---------------------------------------------------------------------------

    _updateChapterNav() {
        const sel = document.getElementById('editor-chapter-nav');
        if (!sel) return;

        const content = this._getContent();
        const lines = content.split('\n');
        const chapters = [];
        let currentChapterWords = 0;

        for (let i = 0; i < lines.length; i++) {
            const m = lines[i].match(/^#\s+(.+)$/);
            if (m) {
                if (chapters.length > 0) {
                    chapters[chapters.length - 1].words = currentChapterWords;
                }
                chapters.push({ title: m[1], line: i, words: 0 });
                currentChapterWords = 0;
            } else {
                currentChapterWords += lines[i].split(/\s+/).filter(Boolean).length;
            }
        }
        if (chapters.length > 0) chapters[chapters.length - 1].words = currentChapterWords;

        this.chapters = chapters;
        sel.innerHTML = '<option value="-1">Chapters</option>' +
            chapters.map((ch, idx) =>
                `<option value="${idx}">${ch.title} (${ch.words.toLocaleString()}w)</option>`
            ).join('');
    },

    _jumpToChapter(idx) {
        if (idx < 0 || idx >= this.chapters.length) return;
        const line = this.chapters[idx].line;

        if (this.cmView) {
            const lineInfo = this.cmView.state.doc.line(line + 1); // CM lines are 1-based
            this.cmView.dispatch({
                selection: { anchor: lineInfo.from },
                effects: CM.EditorView.scrollIntoView(lineInfo.from, { y: 'start' }),
            });
            this.cmView.focus();
        }
        // Reset dropdown
        const sel = document.getElementById('editor-chapter-nav');
        if (sel) sel.value = '-1';
    },

    // ---------------------------------------------------------------------------
    // Input handling
    // ---------------------------------------------------------------------------

    _onInput() {
        const content = this._getContent();
        if (!content && content !== '') return;

        this.isDirty = content !== this.lastSavedContent;
        this._updateStatus(this.isDirty ? 'Unsaved changes' : 'Saved');
        this._updateWordCount(content.split(/\s+/).filter(Boolean).length);

        // Debounced preview — if WYSIWYG is source, still refresh format panels (3+4)
        // but skip panel 2 (the user is actively editing there)
        clearTimeout(this.previewDebounceTimer);
        this.previewDebounceTimer = setTimeout(() => this._requestPreview(), 400);
    },

    // ---------------------------------------------------------------------------
    // Preview
    // ---------------------------------------------------------------------------

    async _requestPreview() {
        const mdPreview = document.getElementById('editor-preview-rendered-body');
        const fmtSource = document.getElementById('editor-preview-source-body');
        const fmtPreview = document.getElementById('editor-preview-fmt-body');
        const sourceHeader = document.getElementById('editor-source-header');
        const fmtPreviewHeader = document.getElementById('editor-fmt-preview-header');
        if (!mdPreview) return;

        let content = this._getContent();
        const MAX_PREVIEW = 500000;
        if (content.length > MAX_PREVIEW) {
            content = content.substring(0, MAX_PREVIEW) + '\n\n[... truncated for preview ...]';
        }

        const thisRequestId = ++this.previewRequestId;
        const fmtLabels = { 'bbcode': 'BBCode', 'clean_html': 'Clean HTML', 'sofurry_html': 'SoFurry HTML', 'styled_html': 'Styled HTML' };

        try {
            // Save scroll positions before re-rendering
            const savedScrolls = {};
            for (const id of ['editor-preview-rendered-body', 'editor-preview-source-body', 'editor-preview-fmt-body']) {
                const el = document.getElementById(id);
                if (el) savedScrolls[id] = el.scrollTop;
            }
            // Save iframe internal scroll (styled_html)
            let savedIframePct = 0;
            if (this.previewFormat === 'styled_html' && fmtPreview) {
                try {
                    const oldIframe = fmtPreview.querySelector('iframe');
                    const iDoc = oldIframe?.contentDocument?.documentElement;
                    if (iDoc && iDoc.scrollHeight > iDoc.clientHeight) {
                        savedIframePct = iDoc.scrollTop / (iDoc.scrollHeight - iDoc.clientHeight);
                    }
                } catch {}
            }

            [mdPreview, fmtSource, fmtPreview].forEach(el => { if (el) el.style.opacity = '0.6'; });

            // 2 parallel requests: MD preview (clean_html) + selected format
            const url = `/api/editor/stories/${encodeURIComponent(this.storyName)}/preview`;
            // Pass live theme vars for styled_html so preview reflects GUI changes
            const fmtBody = { content, format: this.previewFormat };
            if (this.previewFormat === 'styled_html' && Object.keys(this.themeVars).length > 0) {
                fmtBody.theme = this.themeVars;
            }
            const [mdResp, fmtResp] = await Promise.all([
                fetch(url, {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ content, format: 'clean_html' }),
                }),
                fetch(url, {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(fmtBody),
                }),
            ]);

            if (thisRequestId !== this.previewRequestId) return;

            // Parse responses once
            const mdData = mdResp.ok ? await mdResp.json() : null;
            const fmtData = fmtResp.ok ? await fmtResp.json() : null;

            // Panel 2: WYSIWYG editor (contenteditable)
            // Skip panel 2 update if the user is actively editing in it
            if (this._wysiwygEditSource !== 'wysiwyg') {
                if (mdData) {
                    this._wysiwygEditSource = 'cm';
                    const html = mdData.html || '';
                    // Wrap front matter (everything before first <hr />) as non-editable
                    const hrIdx = html.indexOf('<hr');
                    if (hrIdx > 0) {
                        const frontHtml = html.substring(0, hrIdx);
                        const bodyHtml = html.substring(hrIdx);
                        mdPreview.innerHTML = '<div class="preview-html">' +
                            '<div contenteditable="false" class="wysiwyg-frontmatter">' + frontHtml + '</div>' +
                            bodyHtml + '</div>';
                    } else {
                        mdPreview.innerHTML = '<div class="preview-html">' + html + '</div>';
                    }
                    setTimeout(() => { this._wysiwygEditSource = null; }, 0);
                } else {
                    mdPreview.innerHTML = `<p style="color:var(--color-error)">MD preview failed</p>`;
                }
            }

            // Panel 3: Format source (syntax highlighted, read-only)
            if (fmtData && fmtSource) {
                const raw = fmtData.html || '(empty)';
                const label = fmtLabels[fmtData.format] || fmtData.format;
                if (sourceHeader) sourceHeader.textContent = `${label} Source (${raw.length.toLocaleString()} bytes)`;
                const lang = (this.previewFormat === 'bbcode') ? 'bbcode' : 'html';
                if (this.cmSourceView) {
                    this._updateCmContent(this.cmSourceView, raw);
                } else if (typeof CM !== 'undefined') {
                    this.cmSourceView = this._createCmInstance(fmtSource, raw, lang, true);
                    // Attach scroll + selection sync to the new CM source view
                    const srcScroller = this.cmSourceView.dom.querySelector('.cm-scroller');
                    if (srcScroller) {
                        srcScroller.addEventListener('scroll', () => this._syncScroll('cm-source'));
                        srcScroller.addEventListener('mouseup', () => this._syncSelectionFromCMSource());
                    }
                } else {
                    const escaped = raw.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
                    fmtSource.innerHTML = `<pre class="preview-source">${escaped}</pre>`;
                }
            }

            // Panel 4: Format rendered preview
            if (fmtPreview) {
                const label = fmtLabels[this.previewFormat] || this.previewFormat;
                if (fmtPreviewHeader) fmtPreviewHeader.textContent = `${label} Preview`;

                if (this.previewFormat === 'clean_html' && mdData) {
                    fmtPreview.innerHTML = '<div class="preview-html">' + (mdData.html || '') + '</div>';
                } else if (fmtData) {
                    if (this.previewFormat === 'styled_html') {
                        // Reuse existing iframe if possible (avoids full recreate + scroll loss)
                        let iframe = fmtPreview.querySelector('iframe.preview-iframe');
                        if (!iframe) {
                            fmtPreview.innerHTML = '<iframe class="preview-iframe" sandbox="allow-same-origin"></iframe>';
                            iframe = fmtPreview.querySelector('iframe');
                        }
                        iframe.srcdoc = fmtData.preview_html || fmtData.html || '';
                    } else if (this.previewFormat === 'bbcode') {
                        fmtPreview.innerHTML = '<div class="preview-html">' + this._bbcodeToHtml(fmtData.html || '') + '</div>';
                    } else {
                        fmtPreview.innerHTML = '<div class="preview-html">' + (fmtData.html || '') + '</div>';
                    }
                }
            }

            // Sync CSS source view if theme editor is in source mode and styled_html returned CSS
            if (fmtData && fmtData.css && this.themeSourceMode && this.cmCssView) {
                this._updateCmContent(this.cmCssView, fmtData.css);
            }

            [mdPreview, fmtSource, fmtPreview].forEach(el => { if (el) el.style.opacity = '1'; });

            // Restore scroll positions after re-rendering
            for (const [id, pos] of Object.entries(savedScrolls)) {
                const el = document.getElementById(id);
                if (el) el.scrollTop = pos;
            }
            // For styled_html iframe, restore internal scroll after it loads
            if (this.previewFormat === 'styled_html' && fmtPreview && savedIframePct > 0) {
                const iframe = fmtPreview.querySelector('iframe');
                if (iframe) {
                    iframe.addEventListener('load', () => {
                        try {
                            const iDoc = iframe.contentDocument?.documentElement;
                            if (iDoc) {
                                iDoc.scrollTop = savedIframePct * (iDoc.scrollHeight - iDoc.clientHeight);
                            }
                        } catch {}
                    }, { once: true });
                }
            }
        } catch (err) {
            if (mdPreview) mdPreview.innerHTML = `<p style="color:var(--color-error)">Error: ${err.message}</p>`;
            [mdPreview, fmtSource, fmtPreview].forEach(el => { if (el) el.style.opacity = '1'; });
        }
    },

    switchFormat(fmt) {
        this.previewFormat = fmt;
        // Destroy source CM so it gets recreated with the right language
        if (this.cmSourceView) { this.cmSourceView.destroy(); this.cmSourceView = null; }
        clearTimeout(this.previewDebounceTimer);
        this._requestPreview();
    },

    _bbcodeToHtml(bbcode) {
        // Minimal BBCode→HTML for preview rendering
        let html = Utils.escapeHtml(bbcode);
        html = html.replace(/\[b\](.*?)\[\/b\]/gs, '<strong>$1</strong>');
        html = html.replace(/\[i\](.*?)\[\/i\]/gs, '<em>$1</em>');
        html = html.replace(/\[center\](.*?)\[\/center\]/gs, '<div style="text-align:center">$1</div>');
        html = html.replace(/\[color=(.*?)\](.*?)\[\/color\]/gs, '<span style="color:$1">$2</span>');
        html = html.replace(/\[right\](.*?)\[\/right\]/gs, '<div style="text-align:right">$1</div>');
        html = html.replace(/\[left\](.*?)\[\/left\]/gs, '<div style="text-align:left">$1</div>');
        html = html.replace(/\[t\](.*?)\[\/t\]/gs, '<h2 style="text-align:center">$1</h2>');
        // Line breaks
        html = html.replace(/\n/g, '<br>');
        return html;
    },

    // ---------------------------------------------------------------------------
    // Save
    // ---------------------------------------------------------------------------

    async save() {
        const content = this._getContent();

        this._updateStatus('Saving...');
        try {
            const resp = await fetch(`/api/editor/stories/${encodeURIComponent(this.storyName)}/content`, {
                method: 'PUT',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    content: content,
                    expected_mtime: this.lastMtime,
                }),
            });

            if (resp.status === 409) {
                this._updateStatus('Conflict! File changed externally. Reload to merge.');
                return;
            }

            const data = await resp.json();
            if (data.ok) {
                this.lastSavedContent = content;
                this.lastMtime = data.last_modified;
                this.isDirty = false;
                this._updateStatus('Saved');
                this._updateWordCount(data.word_count);
                this._updateChapterNav();
                this._requestSlopScore();
                // Clear recovery draft on successful save
                localStorage.removeItem(`editor_recovery_${this.storyName}`);
            } else {
                this._updateStatus('Save failed');
            }
        } catch (err) {
            this._updateStatus(`Save error: ${err.message}`);
        }
    },

    // ---------------------------------------------------------------------------
    // Format Source
    // ---------------------------------------------------------------------------

    async formatSource() {
        if (typeof html_beautify === 'undefined' && typeof css_beautify === 'undefined') {
            this._updateStatus('Formatter not loaded');
            return;
        }

        const opts = { indent_size: 4, wrap_line_length: 0, preserve_newlines: true, max_preserve_newlines: 2 };
        const cssOpts = { indent_size: 4 };
        let formatted = false;

        // Format the CM source view (panel 3) — HTML or BBCode
        if (this.cmSourceView) {
            const content = this.cmSourceView.state.doc.toString();
            const isHtml = content.includes('<') && content.includes('>');
            if (isHtml && typeof html_beautify !== 'undefined') {
                const pretty = html_beautify(content, opts);
                this._updateCmContent(this.cmSourceView, pretty);
                formatted = true;

                // Save formatted content to disk
                this._updateStatus('Formatting + saving...');
                try {
                    const resp = await fetch(`/api/editor/stories/${encodeURIComponent(this.storyName)}/format-file`, {
                        method: 'PUT',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ format: this.previewFormat, content: pretty }),
                    });
                    if (resp.ok) {
                        const data = await resp.json();
                        this._updateStatus(`Formatted + saved ${data.file} (${data.bytes.toLocaleString()}b)`);
                    } else {
                        const errText = await resp.text();
                        let detail = `HTTP ${resp.status}`;
                        try { const j = JSON.parse(errText); detail = j.detail || j.error || detail; } catch {}
                        this._updateStatus(`Formatted (save failed: ${detail})`);
                    }
                } catch (err) {
                    this._updateStatus(`Formatted (save error: ${err.message})`);
                }
                return;
            }
        }

        // Format the CSS editor if open
        if (this.cmCssView && this.themeSourceMode && typeof css_beautify !== 'undefined') {
            const content = this.cmCssView.state.doc.toString();
            const pretty = css_beautify(content, cssOpts);
            this._updateCmContent(this.cmCssView, pretty);
            formatted = true;
        }

        // Format the MD source (panel 1) — light cleanup only
        if (this.cmView && !formatted) {
            const content = this.cmView.state.doc.toString();
            const cleaned = content
                .split('\n')
                .map(line => line.trimEnd())
                .join('\n')
                .replace(/\n{3,}/g, '\n\n')
                .trim() + '\n';
            if (cleaned !== content) {
                this.cmView.dispatch({
                    changes: { from: 0, to: this.cmView.state.doc.length, insert: cleaned },
                });
                formatted = true;
            }
        }

        this._updateStatus(formatted ? 'Formatted' : 'Nothing to format');
    },

    // ---------------------------------------------------------------------------
    // Regenerate
    // ---------------------------------------------------------------------------

    async regenerate(formats) {
        // formats: null = all (skip_pdf still true), or array e.g. ["html"], ["bbcode"]
        const btn = document.getElementById('editor-regen-btn');
        if (btn) btn.disabled = true;

        // Save first if dirty
        if (this.isDirty) {
            await this.save();
        }

        const label = formats ? formats.join(', ') : 'all';
        this._updateStatus(`Regenerating ${label}...`);
        try {
            const body = { skip_pdf: true };
            if (formats) {
                body.formats = formats;
                // If explicitly requesting PDF, don't skip it
                if (formats.includes('pdf')) body.skip_pdf = false;
            }
            const resp = await fetch(`/api/editor/stories/${encodeURIComponent(this.storyName)}/regenerate`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(body),
            });
            const data = await resp.json();

            // Status field stays terse — the full per-file report used
            // to be dumped here and pushed everything else off-screen.
            // Toast carries the headline; console keeps the detail for
            // anyone debugging via DevTools; the Downloads dropdown
            // shows the canonical post-regen file list.
            if (data.ok) {
                const count = (data.results || []).length;
                const wordSuffix = data.word_count ? ` · ${data.word_count.toLocaleString()} words` : '';
                this._updateStatus('Loaded');
                console.info('[regen]', data.results);
                if (window.toast) {
                    window.toast.success(`Regenerated ${count} format${count === 1 ? '' : 's'}${wordSuffix}`);
                }
            } else {
                const errs = data.errors || [];
                this._updateStatus('Regen errors');
                console.warn('[regen errors]', errs);
                if (window.toast) {
                    const first = errs[0] || 'unknown error';
                    const more = errs.length > 1 ? ` (+${errs.length - 1} more)` : '';
                    window.toast.error(`Regen failed: ${first}${more}`);
                }
            }
            // File set may have changed — invalidate downloads menu so
            // a fresh fetch happens on next open.
            const dlMenu = document.getElementById('downloads-dropdown-menu');
            if (dlMenu) {
                delete dlMenu.dataset.populated;
                dlMenu.innerHTML = '<div class="downloads-loading" style="padding:0.4em 0.75em;color:#888">Loading…</div>';
            }
        } catch (err) {
            this._updateStatus('Regen error');
            console.error('[regen exception]', err);
            if (window.toast) {
                window.toast.error(`Regen error: ${err.message || err}`);
            }
        } finally {
            if (btn) btn.disabled = false;
        }
    },

    // ---------------------------------------------------------------------------
    // Beta-reader draft share (gap-wave-5 §3)
    // ---------------------------------------------------------------------------

    /**
     * Open the "Share this draft" modal: mint / list / revoke read-only public
     * links to preview a story draft. Built as a self-contained overlay (the
     * editor's other dialogs live in the page template; this one is created on
     * demand so it costs nothing until used).
     */
    async _openShareDraft(storyName) {
        document.getElementById('share-draft-overlay')?.remove();
        const overlay = document.createElement('div');
        overlay.className = 'create-story-overlay open';
        overlay.id = 'share-draft-overlay';
        overlay.innerHTML = `
            <div class="create-story-dialog share-draft-dialog" role="dialog" aria-modal="true" aria-label="Share draft" style="max-width:560px;width:92vw">
                <h3>&#128279; Share this draft</h3>
                <p style="color:var(--text-secondary);font-size:0.9rem;margin-bottom:12px">
                    Creates a <strong>public, read-only</strong> link anyone can open — no login needed.
                    They'll see this story as a clean reading page. Handy for beta readers.
                    You can revoke a link at any time.
                </p>
                <div class="share-draft-controls" style="display:flex;gap:8px;align-items:flex-end;margin-bottom:14px">
                    <label class="create-story-label" style="flex:0 0 auto;margin:0">
                        Expires
                        <select id="share-expires" class="create-story-input">
                            <option value="0">Never</option>
                            <option value="7">In 7 days</option>
                            <option value="30">In 30 days</option>
                            <option value="90">In 90 days</option>
                        </select>
                    </label>
                    <button id="share-create-btn" class="btn btn-sm">Create link</button>
                </div>
                <div id="share-list" class="share-draft-list">
                    <div style="color:var(--text-muted);font-size:0.85rem">Loading existing links…</div>
                </div>
                <div class="create-story-actions" style="margin-top:14px">
                    <button class="btn btn-sm btn-outline" id="share-close-btn">Close</button>
                </div>
            </div>`;
        document.body.appendChild(overlay);

        const close = () => overlay.remove();
        overlay.addEventListener('click', (e) => { if (e.target === overlay) close(); });
        overlay.addEventListener('keydown', (e) => { if (e.key === 'Escape') close(); });
        document.getElementById('share-close-btn').addEventListener('click', close);

        const renderList = (shares) => {
            const list = document.getElementById('share-list');
            if (!list) return;
            if (!shares.length) {
                list.innerHTML = `<div style="color:var(--text-muted);font-size:0.85rem">No active links yet — create one above.</div>`;
                return;
            }
            list.innerHTML = shares.map(s => {
                const abs = /^https?:/i.test(s.url) ? s.url : (location.origin + s.url);
                const exp = s.expires_at
                    ? `expires ${new Date(s.expires_at).toLocaleDateString()}`
                    : 'never expires';
                const stale = s.live === false ? ' · <span style="color:var(--danger)">expired</span>' : '';
                return `<div class="share-draft-row" data-token="${Utils.escapeHtml(s.token)}"
                        style="display:flex;gap:6px;align-items:center;margin-bottom:8px;flex-wrap:wrap">
                    <input class="share-draft-url create-story-input" type="text" readonly
                           value="${Utils.escapeHtml(abs)}" style="flex:1 1 220px;min-width:180px" />
                    <button class="btn btn-sm share-copy">Copy</button>
                    <button class="btn btn-sm btn-outline share-revoke">Revoke</button>
                    <span style="flex-basis:100%;color:var(--text-muted);font-size:0.78rem">${exp}${stale}</span>
                </div>`;
            }).join('');
            list.querySelectorAll('.share-copy').forEach(btn => {
                btn.addEventListener('click', () => {
                    const url = btn.closest('.share-draft-row').querySelector('.share-draft-url').value;
                    if (navigator.clipboard) {
                        navigator.clipboard.writeText(url).then(
                            () => window.toast?.success('Link copied'),
                            () => window.toast?.error('Copy failed — select the link and copy manually'));
                    } else {
                        const inp = btn.closest('.share-draft-row').querySelector('.share-draft-url');
                        inp.select(); document.execCommand('copy');
                        window.toast?.success('Link copied');
                    }
                });
            });
            list.querySelectorAll('.share-revoke').forEach(btn => {
                btn.addEventListener('click', async () => {
                    const token = btn.closest('.share-draft-row').dataset.token;
                    try {
                        await API.revokeShareLink(token);
                        window.toast?.info('Link revoked');
                        load();
                    } catch (err) { window.toast?.error('Revoke failed'); }
                });
            });
        };

        const load = async () => {
            try {
                const data = await API.listShareLinks(storyName);
                renderList(data.shares || []);
            } catch (err) {
                const list = document.getElementById('share-list');
                if (list) list.innerHTML = `<div style="color:var(--danger);font-size:0.85rem">Couldn't load existing links.</div>`;
            }
        };

        document.getElementById('share-create-btn').addEventListener('click', async () => {
            const days = parseInt(document.getElementById('share-expires').value) || 0;
            try {
                await API.createShareLink(storyName, days || null);
                window.toast?.success('Share link created');
                load();
            } catch (err) { window.toast?.error('Could not create link'); }
        });

        await load();
    },

    // ---------------------------------------------------------------------------
    // Downloads dropdown
    // ---------------------------------------------------------------------------

    async _populateDownloadsMenu() {
        const menu = document.getElementById('downloads-dropdown-menu');
        if (!menu) return;
        // Tag the menu so the downloads-specific CSS rules apply.
        menu.classList.add('downloads');

        // Per-format friendly label + render order (top to bottom in
        // the dropdown). Per-chapter formats (chapter_bbcode, the
        // chapter-only squidgeworld) are deliberately excluded —
        // anyone wanting individual chapters can grab the whole-story
        // zip and cherry-pick from there.
        const FORMAT_LABELS = {
            epub: 'EPUB',
            pdf: 'PDF',
            styled_html: 'Styled HTML',
            html: 'Clean HTML',
            sofurry_html: 'SoFurry HTML',
            bbcode: 'BBCode',
            markdown: 'Markdown',
        };
        const FORMAT_ORDER = ['epub', 'pdf', 'styled_html', 'html',
                              'sofurry_html', 'bbcode', 'markdown'];

        const fmtSize = (bytes) => {
            if (!bytes) return '';
            if (bytes < 1024) return `${bytes} B`;
            if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
            return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
        };
        const isChapterFile = (path) =>
            path.startsWith('Chapters/') || path.includes('/Chapters/');

        const archiveUrl = `/api/posting/archive?story=${encodeURIComponent(this.storyName)}`;
        const zipEntry =
            `<a class="downloads-row downloads-zip" href="${archiveUrl}" download ` +
            `title="Entire story folder as a single zip">` +
            `<span>Whole story (.zip)</span>` +
            `<span class="downloads-row-size">all formats</span></a>`;

        try {
            // Reuse the published-story endpoint — it already enriches
            // every declared format with file size + modified time.
            const resp = await fetch(`/api/posting/stories/${encodeURIComponent(this.storyName)}`);
            if (!resp.ok) {
                menu.innerHTML =
                    `<div class="downloads-empty">Failed to load formats (${resp.status})</div>` + zipEntry;
                return;
            }
            const data = await resp.json();
            const formats = data.formats || {};

            // One row per format: take the first file in the list,
            // skipping if it's a chapter split (the format-pattern
            // map orders whole-story files first, so files[0] is
            // reliably the canonical full-story file when one exists).
            const items = [];
            for (const fmtKey of FORMAT_ORDER) {
                const meta = formats[fmtKey];
                if (!meta || !meta.available || !meta.files || meta.files.length === 0) continue;
                const file = meta.files[0];
                if (isChapterFile(file.path)) continue;
                const url = `/api/posting/file?story=${encodeURIComponent(this.storyName)}&file=${encodeURIComponent(file.path)}`;
                items.push(
                    `<a class="downloads-row" href="${url}" download title="${file.path}">` +
                    `<span>${FORMAT_LABELS[fmtKey]}</span>` +
                    `<span class="downloads-row-size">${fmtSize(file.size)}</span></a>`
                );
                // EPUB gets a follow-up "Preview" row that opens the
                // in-app viewer in a new tab. Separate row so we don't
                // nest <a> inside <a> (invalid HTML).
                if (fmtKey === 'epub') {
                    const viewerUrl = `/epub-viewer.html?story=${encodeURIComponent(this.storyName)}&file=${encodeURIComponent(file.path)}`;
                    items.push(
                        `<a class="downloads-row downloads-row-sub" href="${viewerUrl}" ` +
                        `target="_blank" rel="noopener" title="Open in EPUB viewer">` +
                        `<span>&nbsp;&nbsp;&#x2197; Preview in browser</span>` +
                        `<span class="downloads-row-size">opens viewer</span></a>`
                    );
                }
            }

            menu.innerHTML = items.length
                ? items.join('') + zipEntry
                : `<div class="downloads-empty">No formats generated yet — try Regenerate first.</div>` + zipEntry;
            menu.dataset.populated = '1';
        } catch (err) {
            menu.innerHTML =
                `<div class="downloads-empty">Error: ${err.message}</div>` + zipEntry;
        }
    },

    // ---------------------------------------------------------------------------
    // UI helpers
    // ---------------------------------------------------------------------------

    _updateStatus(text) {
        const el = document.getElementById('editor-status');
        if (el) el.textContent = text;
    },

    _updateWordCount(count) {
        const el = document.getElementById('editor-wordcount');
        if (el) el.textContent = `${(count || 0).toLocaleString()} words`;
    },

    // ---------------------------------------------------------------------------
    // Slop score
    // ---------------------------------------------------------------------------

    async _requestSlopScore() {
        const el = document.getElementById('editor-slop');
        if (!el) return;

        try {
            const resp = await fetch(`/api/editor/stories/${encodeURIComponent(this.storyName)}/slop`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ content: this._getContent() }),
            });
            if (!resp.ok) { el.textContent = 'Slop: ?'; return; }
            const data = await resp.json();
            this.slopScore = data;

            // Distinguish "scorer unavailable" from a genuine 0.0. A
            // missing word/trigram bundle reads as perfectly clean prose
            // otherwise — silently misleading.
            if (data.available === false) {
                el.innerHTML = `<span style="color:var(--text-muted)" title="Slop scorer data files not loaded — see server logs">Slop: —</span>`;
                return;
            }

            const score = data.score.toFixed(1);
            const rating = data.rating;
            let color = 'var(--color-success)';
            if (rating === 'BORDERLINE') color = 'var(--color-warning)';
            if (rating === 'SLOP') color = 'var(--color-error)';
            el.innerHTML = `<span style="color:${color}" title="${rating}: ${Object.keys(data.word_hits || {}).slice(0, 5).join(', ')}">Slop: ${score}</span>`;
        } catch (err) {
            el.textContent = 'Slop: error';
        }
    },

    // ---------------------------------------------------------------------------
    // Panel visibility toggles
    // ---------------------------------------------------------------------------

    togglePanel(panelId) {
        const panel = document.getElementById(panelId);
        if (!panel) return;

        if (this.hiddenPanels.has(panelId)) {
            // Show
            this.hiddenPanels.delete(panelId);
            panel.style.display = '';
        } else {
            // Hide
            this.hiddenPanels.add(panelId);
            panel.style.display = 'none';
        }
        this._updateGridColumns();
        this._updateRestoreBar();
    },

    _updateGridColumns() {
        const quad = document.getElementById('editor-quad');
        if (!quad) return;
        // Mobile single-panel mode is class-driven (.mobile-active); the
        // grid-template-columns dance only matters on desktop where the
        // user can hide individual panels via the eye icon.
        if (App.isMobileLayoutActive && App.isMobileLayoutActive()) {
            quad.style.gridTemplateColumns = '';
            return;
        }
        const visible = quad.querySelectorAll('.editor-quad-panel:not([style*="display: none"])').length;
        quad.style.gridTemplateColumns = Array(visible).fill('1fr').join(' ');
    },

    /* Mobile single-panel switcher. Adds .mobile-active to one panel,
     * removes it from all others. CSS handles visibility. CodeMirror
     * panes need a measure poke after becoming visible — when they're
     * mounted hidden CM6 records zero size and gutters render wrong
     * until the next layout change. */
    setMobileActivePanel(panelId) {
        document.querySelectorAll('.editor-mobile-tab').forEach(t => {
            t.classList.toggle('active', t.dataset.panel === panelId);
        });
        document.querySelectorAll('.editor-quad-panel').forEach(p => {
            p.classList.toggle('mobile-active', p.id === panelId);
        });
        // Re-measure CodeMirror after the panel becomes visible.
        // requestAnimationFrame waits for layout to settle.
        if (panelId === 'panel-md-code' && this.cmView) {
            requestAnimationFrame(() => {
                try { this.cmView.requestMeasure(); } catch (e) { /* ignore */ }
            });
        }
        if (panelId === 'panel-css-editor' && this.cmCssView) {
            requestAnimationFrame(() => {
                try { this.cmCssView.requestMeasure(); } catch (e) { /* ignore */ }
            });
        }
    },

    _updateRestoreBar() {
        let bar = document.getElementById('panel-restore-bar');
        if (this.hiddenPanels.size === 0) {
            if (bar) bar.remove();
            return;
        }
        if (!bar) {
            bar = document.createElement('div');
            bar.id = 'panel-restore-bar';
            bar.className = 'panel-restore-bar';
            const toolbar = document.querySelector('.editor-toolbar');
            if (toolbar) toolbar.after(bar);
        }
        const labels = {
            'panel-md-code': 'MD Source',
            'panel-md-preview': 'MD Preview',
            'panel-fmt-source': 'Format Source',
            'panel-fmt-preview': 'Format Preview',
            'panel-css-editor': 'CSS',
        };
        bar.innerHTML = 'Hidden: ' + [...this.hiddenPanels].map(id =>
            `<button class="restore-btn" data-restore="${id}">&#128065;&#8203;&#822; ${labels[id] || id}</button>`
        ).join('');
        bar.querySelectorAll('.restore-btn').forEach(btn => {
            btn.addEventListener('click', () => this.togglePanel(btn.dataset.restore));
        });
    },

    // ---------------------------------------------------------------------------
    // WYSIWYG Editor
    // ---------------------------------------------------------------------------

    _initTurndown() {
        if (typeof TurndownService === 'undefined') return;
        this._turndown = new TurndownService({
            headingStyle: 'atx',
            hr: '---',
            emDelimiter: '*',
            strongDelimiter: '**',
            bulletListMarker: '-',
        });

        // Chapter headings: centered <strong> paragraphs → # Heading
        this._turndown.addRule('chapterHeading', {
            filter: (node) => {
                if (node.nodeName !== 'P') return false;
                const style = node.getAttribute('style') || '';
                if (!style.includes('text-align:center') && !style.includes('text-align: center')) return false;
                const children = node.childNodes;
                return children.length === 1 && children[0].nodeName === 'STRONG';
            },
            replacement: (content, node) => {
                const text = node.textContent.trim();
                return `\n# ${text}\n`;
            },
        });

        // Centered paragraphs (subtitles, etc) — preserve as italic centered
        this._turndown.addRule('centeredParagraph', {
            filter: (node) => {
                if (node.nodeName !== 'P') return false;
                const style = node.getAttribute('style') || '';
                if (!style.includes('text-align:center') && !style.includes('text-align: center')) return false;
                const children = node.childNodes;
                // Only match single <em> child (subtitles)
                return children.length === 1 && children[0].nodeName === 'EM';
            },
            replacement: (content, node) => {
                return `\n*${node.textContent.trim()}*\n`;
            },
        });

        // Section breaks
        this._turndown.addRule('sectionBreak', {
            filter: (node) => {
                if (node.nodeName !== 'P' && node.nodeName !== 'DIV') return false;
                return (node.getAttribute('class') || '').includes('section-break') ||
                    (node.textContent.trim().match(/^[*·✦\s]+$/) && (node.getAttribute('style') || '').includes('center'));
            },
            replacement: () => '\n---\n',
        });

        // Non-editable front matter — skip entirely
        this._turndown.addRule('frontMatterBlock', {
            filter: (node) => {
                return node.getAttribute && node.getAttribute('contenteditable') === 'false';
            },
            replacement: () => '',
        });

        // HR elements
        this._turndown.addRule('hrRule', {
            filter: 'hr',
            replacement: () => '\n---\n',
        });
    },

    _cacheFrontMatter(markdown) {
        const lines = markdown.split('\n');
        let bodyIdx = -1;
        for (let i = 0; i < lines.length; i++) {
            if (lines[i].trim() === '<!-- @body -->') { bodyIdx = i; break; }
        }
        if (bodyIdx >= 0) {
            // Include the @body line and the --- after it
            let endIdx = bodyIdx;
            for (let i = bodyIdx + 1; i < lines.length && i <= bodyIdx + 2; i++) {
                if (lines[i].trim() === '---' || lines[i].trim() === '') endIdx = i;
                else break;
            }
            this._frontMatterMd = lines.slice(0, endIdx + 1).join('\n');
            this._bodyStartLine = endIdx + 1;
        } else {
            this._frontMatterMd = '';
            this._bodyStartLine = 0;
        }
    },

    _ANCHOR_HINTS: {
        'title': {
            label: 'Title  — @title',
            purpose: 'Marks the line immediately below as the story title.',
            before: '# Late Shift',
            after: '<!-- @title -->\n# Late Shift',
        },
        'subtitle': {
            label: 'Subtitle  — @subtitle',
            purpose: 'Marks the line below as a subtitle or tagline.',
            before: '*A Convenience Store Romance*',
            after: '<!-- @subtitle -->\n*A Convenience Store Romance*',
        },
        'byline': {
            label: 'Byline  — @byline',
            purpose: 'Author attribution line. Rendered centered below the title/subtitle.',
            before: '*by KnaughtyKat*',
            after: '<!-- @byline -->\n*by KnaughtyKat*',
        },
        'warning': {
            label: 'Content Warning  — @warning',
            purpose: 'Opens a content-warning block. Everything below (until the next anchor) is the warning.',
            before: '**Content Warning**: Explicit content, themes, etc.',
            after: '<!-- @warning -->\n**Content Warning**: Explicit content, themes, etc.',
        },
        'disclaimer': {
            label: 'Disclaimer  — @disclaimer',
            purpose: 'Opens a disclaimer block (fiction notice, character age attestation, etc.). Runs until the next anchor.',
            before: '**DISCLAIMER**\n\nThis is a work of fiction...',
            after: '<!-- @disclaimer -->\n**DISCLAIMER**\n\nThis is a work of fiction...',
        },
        'fanfiction': {
            label: 'Fan Fiction Notice  — @fanfiction',
            purpose: 'IP attribution block for fan fiction. Runs until the next anchor.',
            before: '**FAN FICTION NOTICE**\n\nThis story is set in...',
            after: '<!-- @fanfiction -->\n**FAN FICTION NOTICE**\n\nThis story is set in...',
        },
        'body': {
            label: 'Body  — @body',
            purpose: 'Boundary marker. Everything BEFORE is front matter (title page etc.); everything AFTER is the story body.',
            before: '(end of front matter)\n\n# Chapter 1: The Counter',
            after: '<!-- @body -->\n\n# Chapter 1: The Counter',
        },
        'text-sent': {
            label: 'Sent text message  — @text-sent',
            purpose: 'Renders the line below as an outgoing text-message bubble.',
            before: '**Ryan:** *See you at seven.*',
            after: '<!-- @text-sent -->\n**Ryan:** *See you at seven.*',
        },
        'text-received': {
            label: 'Received text message  — @text-received',
            purpose: 'Renders the line below as an incoming text-message bubble.',
            before: '**Marcus:** *On my way.*',
            after: '<!-- @text-received -->\n**Marcus:** *On my way.*',
        },
        'phone-incoming': {
            label: 'Phone display  — @phone-incoming',
            purpose: 'Renders the line below inside a phone-screen frame (caller ID, call status, etc.).',
            before: '**Unknown Caller**',
            after: '<!-- @phone-incoming -->\n**Unknown Caller**',
        },
    },

    _initWysiwygToolbar() {
        const toolbar = document.getElementById('wysiwyg-toolbar');
        if (!toolbar) return;
        toolbar.querySelectorAll('button[data-cmd]').forEach(btn => {
            btn.addEventListener('mousedown', (e) => {
                e.preventDefault();
                this._execWysiwygCmd(btn.dataset.cmd);
            });
        });
        toolbar.querySelectorAll('button[data-anchor]').forEach(btn => {
            btn.addEventListener('mousedown', (e) => {
                e.preventDefault();
                this._insertAnchor(btn.dataset.anchor);
            });
        });
        this._initAnchorTooltips();
    },

    _initAnchorTooltips() {
        let tip = document.getElementById('anchor-tooltip');
        if (!tip) {
            tip = document.createElement('div');
            tip.className = 'anchor-tooltip';
            tip.id = 'anchor-tooltip';
            document.body.appendChild(tip);
        }
        this._anchorTooltipEl = tip;
        this._anchorTooltipTimer = null;

        const toolbar = document.getElementById('wysiwyg-toolbar');
        if (!toolbar) return;
        toolbar.querySelectorAll('button[data-anchor]').forEach(btn => {
            const type = btn.dataset.anchor;
            const hint = this._ANCHOR_HINTS[type];
            if (!hint) return;
            btn.addEventListener('mouseenter', () => {
                clearTimeout(this._anchorTooltipTimer);
                this._anchorTooltipTimer = setTimeout(() => {
                    this._showAnchorTooltip(btn, hint);
                }, 1200);
            });
            btn.addEventListener('mouseleave', () => {
                clearTimeout(this._anchorTooltipTimer);
                this._hideAnchorTooltip();
            });
            btn.addEventListener('mousedown', () => {
                clearTimeout(this._anchorTooltipTimer);
                this._hideAnchorTooltip();
            });
        });
    },

    _showAnchorTooltip(btn, hint) {
        const tip = this._anchorTooltipEl;
        if (!tip) return;
        const esc = Utils.escapeHtml;
        tip.innerHTML =
            `<div class="anchor-tooltip-label">${esc(hint.label)}</div>` +
            `<div class="anchor-tooltip-purpose">${esc(hint.purpose)}</div>` +
            `<h6>Without anchor</h6>` +
            `<pre>${esc(hint.before)}</pre>` +
            `<h6>With anchor</h6>` +
            `<pre class="anchor-tooltip-after">${esc(hint.after)}</pre>`;
        tip.classList.add('visible');
        const rect = btn.getBoundingClientRect();
        const tipW = tip.offsetWidth;
        const tipH = tip.offsetHeight;
        let left = Math.max(8, Math.min(rect.left, window.innerWidth - tipW - 8));
        let top = rect.bottom + 8;
        if (top + tipH > window.innerHeight - 8) {
            top = Math.max(8, rect.top - tipH - 8);
        }
        tip.style.left = `${left}px`;
        tip.style.top = `${top}px`;
    },

    _hideAnchorTooltip() {
        if (this._anchorTooltipEl) {
            this._anchorTooltipEl.classList.remove('visible');
        }
    },

    _insertAnchor(type) {
        if (!this._ANCHOR_HINTS[type] || !this.cmView) return;
        const anchorText = `<!-- @${type} -->`;

        // Resolve the target range. If the Rich Editor has a text selection
        // whose plain-text content appears exactly once in the Markdown
        // source, target that occurrence; otherwise fall back to CodeMirror's
        // own selection/cursor.
        let { from, to } = this.cmView.state.selection.main;
        const wysiwygBody = document.getElementById('editor-preview-rendered-body');
        const winSel = window.getSelection && window.getSelection();
        if (
            wysiwygBody && winSel && winSel.rangeCount &&
            wysiwygBody.contains(winSel.anchorNode) &&
            winSel.toString().length > 0
        ) {
            const picked = winSel.toString();
            const docStr = this.cmView.state.doc.toString();
            const hit = docStr.indexOf(picked);
            if (hit >= 0 && docStr.indexOf(picked, hit + picked.length) === -1) {
                from = hit;
                to = hit + picked.length;
            }
        }

        // All canonical anchors are single-line labels that tag the line(s)
        // immediately below them (the converter reads semantics this way).
        // So: insert `<!-- @foo -->\n` at the start of the line containing
        // `from`. Selections shift down intact; bare cursors end up below
        // the newly-inserted anchor on the original content line.
        const line = this.cmView.state.doc.lineAt(from);
        const insert = anchorText + '\n';
        const shift = insert.length;
        this.cmView.dispatch({
            changes: { from: line.from, insert },
            selection: from !== to
                ? { anchor: from + shift, head: to + shift }
                : { anchor: from + shift },
        });
        this.cmView.focus();
    },

    _execWysiwygCmd(cmd) {
        // Don't call body.focus() — the mousedown preventDefault keeps focus in contenteditable
        switch (cmd) {
            case 'bold':
                document.execCommand('bold', false, null);
                break;
            case 'italic':
                document.execCommand('italic', false, null);
                break;
            case 'undo':
                document.execCommand('undo', false, null);
                break;
            case 'redo':
                document.execCommand('redo', false, null);
                break;
            case 'heading': {
                // Wrap current line/selection as a chapter heading
                const sel = window.getSelection();
                if (!sel.rangeCount) break;
                const text = sel.toString().trim() || 'Chapter Heading';
                document.execCommand('insertHTML', false,
                    `<p style="text-align:center"><strong>${Utils.escapeHtml(text)}</strong></p>`);
                break;
            }
            case 'hr':
                document.execCommand('insertHTML', false, '<hr />');
                break;
        }
    },

    _initWysiwygInput() {
        const body = document.getElementById('editor-preview-rendered-body');
        if (!body) return;

        body.addEventListener('input', () => {
            if (this._wysiwygEditSource === 'cm') return; // ignore CM-triggered updates
            clearTimeout(this._wysiwygSyncTimer);
            this._wysiwygSyncTimer = setTimeout(() => this._syncWysiwygToCM(), 400);
        });

        // Paste handler — sanitize to plain text with basic formatting
        body.addEventListener('paste', (e) => {
            e.preventDefault();
            const text = e.clipboardData.getData('text/plain');
            document.execCommand('insertText', false, text);
        });
    },

    _syncWysiwygToCM() {
        if (!this._turndown || !this.cmView) return;
        const body = document.getElementById('editor-preview-rendered-body');
        if (!body) return;

        this._wysiwygEditSource = 'wysiwyg';

        // Convert HTML → markdown (only the editable body, not front matter)
        let bodyMd = this._turndown.turndown(body.innerHTML);

        // Clean up: normalize multiple blank lines to double
        bodyMd = bodyMd.replace(/\n{3,}/g, '\n\n').trim();

        // Re-extract front matter from current CM content (not stale cache)
        const currentMd = this.cmView.state.doc.toString();
        const bodyMarker = '<!-- @body -->';
        const bodyMarkerIdx = currentMd.indexOf(bodyMarker);
        let frontMatter = '';
        if (bodyMarkerIdx >= 0) {
            // Include @body line + any trailing --- separator
            let endIdx = bodyMarkerIdx + bodyMarker.length;
            const after = currentMd.substring(endIdx);
            const trailMatch = after.match(/^\n(---\n|\n)/);
            if (trailMatch) endIdx += trailMatch[0].length;
            frontMatter = currentMd.substring(0, endIdx);
        }

        // Reconstruct full markdown: front matter + body
        const fullMd = frontMatter
            ? frontMatter + '\n' + bodyMd + '\n'
            : bodyMd + '\n';

        // Save CM scroll position before replacing content
        const cmScroller = this.cmView.dom.querySelector('.cm-scroller');
        const savedScroll = cmScroller ? cmScroller.scrollTop : 0;

        // Update CM editor without triggering a preview refresh
        this.cmView.dispatch({
            changes: {
                from: 0,
                to: this.cmView.state.doc.length,
                insert: fullMd,
            },
        });

        // Restore CM scroll position
        if (cmScroller) cmScroller.scrollTop = savedScroll;

        this.isDirty = true;
        this._updateWordCount(fullMd.split(/\s+/).length);

        // Clear the flag after a microtask so CM's updateListener sees it
        setTimeout(() => { this._wysiwygEditSource = null; }, 0);
    },

    // ---------------------------------------------------------------------------
    // CSS Editor
    // ---------------------------------------------------------------------------

    cssEditorOpen: false,
    themeVars: {},
    themeSavedVars: {},      // snapshot from server — for Revert
    themeHistory: [],        // undo stack
    themeSourceMode: false,  // false = GUI, true = raw CSS source

    async toggleCssEditor() {
        this.cssEditorOpen = !this.cssEditorOpen;
        const quad = document.getElementById('editor-quad');
        let cssPanel = document.getElementById('panel-css-editor');

        if (this.cssEditorOpen) {
            if (!cssPanel) {
                const panel = document.createElement('div');
                panel.className = 'editor-quad-panel editor-css-panel';
                panel.id = 'panel-css-editor';
                panel.innerHTML = `
                    <div class="preview-panel-header">
                        <button class="panel-toggle" data-panel="panel-css-editor" title="Hide panel">&#128065;</button>
                        Theme Editor
                        <button class="btn-tiny" id="theme-save-btn">Save</button>
                        <button class="btn-tiny" id="theme-undo-btn" disabled title="Undo last change">Undo</button>
                        <button class="btn-tiny" id="theme-revert-btn" title="Revert to saved">Revert</button>
                        <button class="btn-tiny" id="theme-source-btn">Source</button>
                    </div>
                    <div id="theme-editor-body" class="preview-panel-body theme-editor-body"></div>
                `;
                quad.appendChild(panel);
                document.getElementById('theme-save-btn')?.addEventListener('click', () => this.saveTheme());
                document.getElementById('theme-undo-btn')?.addEventListener('click', () => this.undoTheme());
                document.getElementById('theme-revert-btn')?.addEventListener('click', () => this.revertTheme());
                document.getElementById('theme-source-btn')?.addEventListener('click', () => this._toggleThemeSource());
                document.querySelector('#panel-css-editor .panel-toggle')?.addEventListener('click', () => this.togglePanel('panel-css-editor'));
                // Add a CSS tab to the mobile switcher and jump to it.
                const tabs = document.getElementById('editor-mobile-tabs');
                if (tabs && !tabs.querySelector('[data-panel="panel-css-editor"]')) {
                    const tab = document.createElement('button');
                    tab.className = 'editor-mobile-tab';
                    tab.type = 'button';
                    tab.dataset.panel = 'panel-css-editor';
                    tab.textContent = 'CSS';
                    tab.addEventListener('click', () => this.setMobileActivePanel('panel-css-editor'));
                    tabs.appendChild(tab);
                }
            }
            await this._loadThemeEditor();
            this._updateGridColumns();
            if (App.isMobileLayoutActive && App.isMobileLayoutActive()) {
                this.setMobileActivePanel('panel-css-editor');
            }
        } else {
            if (this.cmCssView) { this.cmCssView.destroy(); this.cmCssView = null; }
            if (cssPanel) cssPanel.remove();
            // Remove the CSS tab from the mobile switcher.
            document.querySelector('#editor-mobile-tabs [data-panel="panel-css-editor"]')?.remove();
            this._updateGridColumns();
            // If the CSS tab was the active one, fall back to Edit.
            if (App.isMobileLayoutActive && App.isMobileLayoutActive()) {
                const stillActive = document.querySelector('.editor-quad-panel.mobile-active');
                if (!stillActive) this.setMobileActivePanel('panel-md-code');
            }
        }
    },

    async _loadThemeEditor() {
        const body = document.getElementById('theme-editor-body');
        if (!body) return;

        try {
            const resp = await fetch(`/api/editor/stories/${encodeURIComponent(this.storyName)}/theme`);
            const data = await resp.json();
            this.themeVars = data.variables || {};
            this.themeSavedVars = { ...this.themeVars };
            this.themeHistory = [];
            if (data.error) { this._updateStatus(`Theme: ${data.error}`); return; }
            this._renderThemeGUI();
            this._updateUndoBtn();
        } catch (err) {
            body.innerHTML = `<p style="color:var(--color-error)">Error: ${err.message}</p>`;
        }
    },

    _renderThemeGUI() {
        const body = document.getElementById('theme-editor-body');
        if (!body) return;

        const colorRow = (label, key) => {
            const val = this.themeVars[key] || '#000000';
            return `<div class="theme-row">
                <label>${label}</label>
                <input type="color" value="${val.startsWith('#') ? val : '#000000'}" data-key="${key}">
                <input type="text" value="${val}" data-key="${key}" class="theme-hex">
            </div>`;
        };

        const textRow = (label, key, placeholder) => {
            const val = this.themeVars[key] || '';
            return `<div class="theme-row">
                <label>${label}</label>
                <input type="text" value="${val}" data-key="${key}" class="theme-text" placeholder="${placeholder || ''}">
            </div>`;
        };

        const selectRow = (label, key, options) => {
            const val = this.themeVars[key] || options[0]?.value || '';
            const opts = options.map(o => `<option value="${o.value}" ${o.value === val ? 'selected' : ''}>${o.label}</option>`).join('');
            return `<div class="theme-row">
                <label>${label}</label>
                <select data-key="${key}">${opts}</select>
            </div>`;
        };

        body.innerHTML = `
            <div class="theme-section">
                <h4>Colours</h4>
                ${colorRow('Background', 'BACKGROUND')}
                ${colorRow('Body Text', 'TEXT_COLOUR')}
                ${colorRow('Title', 'TITLE_COLOUR')}
                ${colorRow('Byline', 'BYLINE_COLOUR')}
                ${colorRow('Accent', 'ACCENT_COLOUR')}
                ${colorRow('Warning Heading', 'WARNING_HEADING_COLOUR')}
                ${colorRow('Warning Body', 'WARNING_BODY_COLOUR')}
                ${colorRow('Disclaimer', 'DISCLAIMER_HEADING_COLOUR')}
                ${colorRow('Story End', 'STORY_END_COLOUR')}
                ${colorRow('Signature', 'SIGNATURE_COLOUR')}
            </div>
            <div class="theme-section">
                <h4>Text Messages</h4>
                ${colorRow('Sent Background', 'TEXT_SENT_COLOUR')}
                ${colorRow('Received Background', 'TEXT_RECEIVED_COLOUR')}
            </div>
            <div class="theme-section">
                <h4>Typography</h4>
                ${textRow('Title Shadow', 'TITLE_TEXT_SHADOW', 'text-shadow: 0 0 25px rgba(...)')}
            </div>
            <div class="theme-section">
                <h4>Decorations</h4>
                ${this._iconSelector('WARNING_ICON')}
                ${this._breakSelector('SECTION_BREAK_SYMBOL')}
            </div>
            <div class="theme-section">
                <h4>Print</h4>
                ${selectRow('Approach', 'PRINT_APPROACH', [
                    {value: 'colour-preserve', label: 'Colour Preserve (dark bg)'},
                    {value: 'grayscale', label: 'Grayscale (light bg)'},
                ])}
            </div>
        `;

        // Bind custom inputs (icon + break selectors have their own text fields)
        body.querySelectorAll('.theme-combo-select').forEach(sel => {
            const key = sel.dataset.key;
            const textInput = body.querySelector(`.theme-combo-text[data-key="${key}"]`);
            sel.addEventListener('change', () => {
                if (sel.value === '__custom__') {
                    if (textInput) textInput.style.display = '';
                } else {
                    if (textInput) { textInput.style.display = 'none'; textInput.value = sel.value; }
                    this._pushThemeUndo();
                    this.themeVars[key] = sel.value;
                    this._onThemeChange();
                }
            });
            if (textInput) {
                textInput.addEventListener('change', () => {
                    this._pushThemeUndo();
                    this.themeVars[key] = textInput.value;
                    this._onThemeChange();
                });
            }
        });

        // Bind colour picker ↔ hex input sync
        // Colour pickers fire 'input' continuously while dragging — only push
        // one undo entry per drag (on first input), not hundreds.
        body.querySelectorAll('input[type="color"]').forEach(picker => {
            const key = picker.dataset.key;
            const hex = body.querySelector(`.theme-hex[data-key="${key}"]`);
            let dragging = false;
            picker.addEventListener('input', () => {
                if (!dragging) { this._pushThemeUndo(); dragging = true; }
                if (hex) hex.value = picker.value;
                this.themeVars[key] = picker.value;
                this._onThemeChange();
            });
            picker.addEventListener('change', () => { dragging = false; });
        });
        body.querySelectorAll('.theme-hex').forEach(input => {
            const key = input.dataset.key;
            const picker = body.querySelector(`input[type="color"][data-key="${key}"]`);
            input.addEventListener('change', () => {
                this._pushThemeUndo();
                if (picker && input.value.match(/^#[0-9a-fA-F]{6}$/)) picker.value = input.value;
                this.themeVars[key] = input.value;
                this._onThemeChange();
            });
        });
        body.querySelectorAll('.theme-text, select[data-key]').forEach(input => {
            input.addEventListener('change', () => {
                this._pushThemeUndo();
                this.themeVars[input.dataset.key] = input.value;
                this._onThemeChange();
            });
        });
    },

    _iconSelector(key) {
        const val = this.themeVars[key] || '&#9888;';
        const icons = [
            // Classic warnings
            ['&#9888;', '⚠ Warning Triangle'],
            ['&#9762;', '☢ Radioactive'],
            ['&#9763;', '☣ Biohazard'],
            ['&#9760;', '☠ Skull & Crossbones'],
            ['&#10060;', '❌ Cross Mark'],
            ['&#10071;', '❗ Exclamation'],
            // Stars & celestial
            ['&#9733;', '★ Black Star'],
            ['&#9734;', '☆ White Star'],
            ['&#10022;', '✦ Six-Point Star'],
            ['&#10023;', '✧ Open Star'],
            ['&#10038;', '✶ Six-Point Solid'],
            ['&#10041;', '✹ Twelve-Point Star'],
            ['&#10043;', '✻ Heavy Teardrop'],
            ['&#10045;', '✽ Balloon Asterisk'],
            ['&#10037;', '✵ Pinwheel Star'],
            ['&#9789;', '☽ Crescent Moon'],
            ['&#9790;', '☾ Last Quarter Moon'],
            // Geometric
            ['&#9670;', '◆ Black Diamond'],
            ['&#9671;', '◇ White Diamond'],
            ['&#9830;', '♦ Diamond Suit'],
            ['&#10070;', '❖ Black Diamond Minus'],
            ['&#9679;', '● Black Circle'],
            ['&#9675;', '○ White Circle'],
            ['&#9632;', '■ Black Square'],
            ['&#9650;', '▲ Triangle Up'],
            ['&#11044;', '⬤ Large Circle'],
            // Nature & ornamental
            ['&#10048;', '✿ Black Florette'],
            ['&#10049;', '❁ Eight-Petal Flower'],
            ['&#10053;', '❅ Tight Snowflake'],
            ['&#10054;', '❆ Heavy Snowflake'],
            ['&#9884;', '⚜ Fleur-de-lis'],
            ['&#9752;', '☘ Shamrock'],
            ['&#9773;', '☭ Hammer (industrial)'],
            // Hearts & suits
            ['&#9829;', '♥ Heart'],
            ['&#9827;', '♣ Club'],
            ['&#9824;', '♠ Spade'],
            ['&#10084;', '❤ Heavy Heart'],
            // Misc symbols
            ['&#10016;', '✠ Maltese Cross'],
            ['&#10013;', '✝ Latin Cross'],
            ['&#10014;', '✞ Outlined Cross'],
            ['&#9876;', '⚔ Crossed Swords'],
            ['&#9873;', '⚑ Black Flag'],
            ['&#9883;', '⚫ Medium Circle (dark)'],
            ['&#10026;', '✪ Circled Star'],
            ['&#10031;', '✯ Six-Point Pinwheel'],
            // Emoji-style
            ['&#128293;', '🔥 Fire'],
            ['&#128420;', '🖤 Black Heart'],
            ['&#127801;', '🌹 Rose'],
            ['&#128128;', '💀 Skull'],
            ['&#9889;', '⚡ Lightning'],
            ['&#127775;', '🌟 Glowing Star'],
        ];
        const isCustom = !icons.some(([v]) => v === val);
        const opts = icons.map(([v, l]) => `<option value="${v}" ${v === val ? 'selected' : ''}>${l}</option>`).join('');
        return `<div class="theme-row">
            <label>Warning Icon</label>
            <select data-key="${key}" class="theme-combo-select">
                ${opts}
                <option value="__custom__" ${isCustom ? 'selected' : ''}>✏ Custom...</option>
            </select>
            <input type="text" value="${val}" data-key="${key}" class="theme-combo-text theme-text" style="${isCustom ? '' : 'display:none'}" placeholder="HTML entity e.g. &#9888;">
        </div>`;
    },

    _breakSelector(key) {
        const val = this.themeVars[key] || '* &ensp; * &ensp; *';
        const breaks = [
            // Classic
            ['* &ensp; * &ensp; *', '* * * (classic)'],
            ['• • •', '• • • (bullets)'],
            ['· · · · ·', '· · · · · (dots)'],
            ['~ ~ ~', '~ ~ ~ (tildes)'],
            ['— — —', '— — — (em dashes)'],
            // Star patterns
            ['&#10022; &ensp; &#10022; &ensp; &#10022;', '✦ ✦ ✦ (solid stars)'],
            ['&#10023; &ensp; &#10023; &ensp; &#10023;', '✧ ✧ ✧ (open stars)'],
            ['&#9733; &ensp; &#10022; &ensp; &#9733;', '★ ✦ ★ (star trio)'],
            ['· &ensp; &#10022; &ensp; ·', '· ✦ · (dot star dot)'],
            ['~ &ensp; &#10023; &ensp; ~', '~ ✧ ~ (tilde star)'],
            ['&#10022; &#11824; &#10022;', '✦ ⸰ ✦ (star ring star)'],
            ['&#10043; &ensp; &#10043; &ensp; &#10043;', '✻ ✻ ✻ (asterisks)'],
            // Diamond patterns
            ['&mdash; &#10022; &mdash;', '— ✦ — (dash star)'],
            ['&mdash; &#10070; &mdash;', '— ❖ — (dash diamond)'],
            ['&#9670; &middot; &#9670;', '◆ · ◆ (dot diamond)'],
            ['&#9671; &ensp; &#9670; &ensp; &#9671;', '◇ ◆ ◇ (diamonds)'],
            ['&#9830; &ensp; &#9830; &ensp; &#9830;', '♦ ♦ ♦ (suits)'],
            // Celestial
            ['&#9789; &ensp; &#10022; &ensp; &#9790;', '☽ ✦ ☾ (moons + star)'],
            ['&#9789; &ensp; &#9733; &ensp; &#9790;', '☽ ★ ☾ (moon star moon)'],
            ['&#9733; &ensp; &#9789; &ensp; &#9733;', '★ ☽ ★ (star moon star)'],
            ['&#10023; &ensp; &#9789; &ensp; &#10023;', '✧ ☽ ✧ (open star moon)'],
            // Ornamental
            ['&#10023; &#9884; &#10023;', '✧ ⚜ ✧ (star fleur-de-lis)'],
            ['&#8258; &#9830; &#8258;', '⁂ ♦ ⁂ (asterism diamond)'],
            ['&#9884; &ensp; &#9884; &ensp; &#9884;', '⚜ ⚜ ⚜ (three fleur-de-lis)'],
            ['&#10048; &ensp; &#10048; &ensp; &#10048;', '✿ ✿ ✿ (flowers)'],
            ['&#10049; &ensp; &#10049; &ensp; &#10049;', '❁ ❁ ❁ (eight-petal)'],
            ['&#9752; &ensp; &#9752; &ensp; &#9752;', '☘ ☘ ☘ (shamrocks)'],
            // Rules & lines
            ['─── &#10022; ───', '─── ✦ ─── (ruled star)'],
            ['─── &#10070; ───', '─── ❖ ─── (ruled diamond)'],
            ['─── &#9884; ───', '─── ⚜ ─── (ruled fleur-de-lis)'],
            ['═══════', '═══════ (double rule)'],
            ['───────', '─────── (single rule)'],
            ['─ · ─ · ─', '─ · ─ · ─ (morse)'],
            // Singles & pairs
            ['&#8258;', '⁂ (asterism)'],
            ['&#10087;', '❧ (rotated floral)'],
            ['&#10086;', '❦ (floral heart)'],
            ['§', '§ (section sign)'],
            ['&#8224; &ensp; &#8224; &ensp; &#8224;', '† † † (daggers)'],
            ['&#8225; &ensp; &#8225; &ensp; &#8225;', '‡ ‡ ‡ (double daggers)'],
            ['&#8734;', '∞ (infinity)'],
            ['&#9876;', '⚔ (crossed swords)'],
            // Hearts
            ['&#9829; &ensp; &#9829; &ensp; &#9829;', '♥ ♥ ♥ (hearts)'],
            ['&#10084;', '❤ (heavy heart)'],
            ['&#9825; &ensp; &#9829; &ensp; &#9825;', '♡ ♥ ♡ (open heart)'],
            // Arrows & misc
            ['&#10148; &ensp; &#10148; &ensp; &#10148;', '➤ ➤ ➤ (arrows)'],
            ['&#8226; &#10022; &#8226;', '• ✦ • (bullet star)'],
            ['&#10040; &ensp; &#10040; &ensp; &#10040;', '✸ ✸ ✸ (heavy stars)'],
            ['&#10059;', '❋ (heavy asterisk)'],
            ['&#10056; &ensp; &#10056; &ensp; &#10056;', '❈ ❈ ❈ (asterisk flowers)'],
        ];
        const isCustom = !breaks.some(([v]) => v === val);
        const opts = breaks.map(([v, l]) => `<option value="${v}" ${v === val ? 'selected' : ''}>${l}</option>`).join('');
        return `<div class="theme-row">
            <label>Section Break</label>
            <select data-key="${key}" class="theme-combo-select">
                ${opts}
                <option value="__custom__" ${isCustom ? 'selected' : ''}>✏ Custom...</option>
            </select>
            <input type="text" value="${val}" data-key="${key}" class="theme-combo-text theme-text" style="${isCustom ? '' : 'display:none'}" placeholder="HTML entities or text">
        </div>`;
    },

    _pushThemeUndo() {
        // Snapshot current state before a change — cap at 50 entries
        this.themeHistory.push({ ...this.themeVars });
        if (this.themeHistory.length > 50) this.themeHistory.shift();
        this._updateUndoBtn();
    },

    _updateUndoBtn() {
        const btn = document.getElementById('theme-undo-btn');
        if (btn) btn.disabled = this.themeHistory.length === 0;
    },

    undoTheme() {
        if (this.themeHistory.length === 0) return;
        this.themeVars = this.themeHistory.pop();
        this._updateUndoBtn();
        this._renderThemeGUI();
        this._onThemeChange();
    },

    revertTheme() {
        if (Object.keys(this.themeSavedVars).length === 0) return;
        // Push current state so the revert itself is undoable
        this._pushThemeUndo();
        this.themeVars = { ...this.themeSavedVars };
        this._renderThemeGUI();
        this._onThemeChange();
        this._updateStatus('Theme reverted to saved');
    },

    _onThemeChange() {
        // Live preview refresh — always trigger when theme changes so CSS stays in sync
        clearTimeout(this.previewDebounceTimer);
        this.previewDebounceTimer = setTimeout(() => {
            // If styled_html preview is active, preview request carries the theme vars
            // and returns generated CSS which we use to sync the source view
            if (this.previewFormat === 'styled_html') {
                this._requestPreview();
            }
        }, 300);
    },

    _toggleThemeSource() {
        this.themeSourceMode = !this.themeSourceMode;
        const body = document.getElementById('theme-editor-body');
        const btn = document.getElementById('theme-source-btn');
        if (!body) return;

        if (this.themeSourceMode) {
            // Show raw CSS
            if (btn) btn.textContent = 'GUI';
            (async () => {
                const resp = await fetch(`/api/editor/stories/${encodeURIComponent(this.storyName)}/css`);
                const data = await resp.json();
                if (this.cmCssView) { this.cmCssView.destroy(); this.cmCssView = null; }
                this.cmCssView = this._createCmInstance(body, data.css || '', 'css', false);
            })();
        } else {
            // Back to GUI
            if (btn) btn.textContent = 'Source';
            if (this.cmCssView) { this.cmCssView.destroy(); this.cmCssView = null; }
            this._renderThemeGUI();
        }
    },

    async saveTheme() {
        this._updateStatus('Saving theme...');
        try {
            let resp, data;
            if (this.themeSourceMode && this.cmCssView) {
                // Save raw CSS directly
                resp = await fetch(`/api/editor/stories/${encodeURIComponent(this.storyName)}/css`, {
                    method: 'PUT',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ css: this.cmCssView.state.doc.toString() }),
                });
            } else {
                // Save theme variables → regenerate CSS
                const payload = { variables: this.themeVars };
                resp = await fetch(`/api/editor/stories/${encodeURIComponent(this.storyName)}/theme`, {
                    method: 'PUT',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(payload),
                });
            }
            if (!resp.ok) {
                const errText = await resp.text();
                let detail = `HTTP ${resp.status}`;
                try { const j = JSON.parse(errText); detail = j.detail || j.error || detail; } catch {}
                this._updateStatus(`Save failed: ${detail}`);
                return;
            }
            data = await resp.json();
            if (this.themeSourceMode) {
                this._updateStatus(`CSS saved (${data.bytes}b)`);
            } else {
                // Update saved snapshot so Revert goes back to this state
                this.themeSavedVars = { ...this.themeVars };
                this.themeHistory = [];
                this._updateUndoBtn();
                this._updateStatus(`Theme saved (${data.css_bytes}b CSS)`);
            }
            if (this.previewFormat === 'styled_html') this._requestPreview();
        } catch (err) {
            this._updateStatus(`Theme save error: ${err.message}`);
        }
    },

};
