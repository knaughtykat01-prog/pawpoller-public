/**
 * Metadata Editor — slide-in drawer for editing story.json metadata.
 *
 * Phase 1 scope:
 *   - Story Info (title, author, fandom, rating)
 *   - Description & Summary (with character counters)
 *
 * Phase 2 scope:
 *   - Classifications (warnings, categories, characters, relationships)
 *   - Per-Platform Tags (stub textareas — autocomplete in Phase 3)
 *   - Platform Toggles
 *
 * Later phases will add: tag autocomplete (P3), per-chapter editing (P4),
 * cover uploads + raw JSON view (P5).
 */
const MetaEditor = {
    // State
    isOpen: false,
    metadata: null,           // current loaded story.json
    initialMetadata: null,    // snapshot for dirty check
    lastMtime: 0,
    storyName: null,

    // Phase 3a: tag database (lazy-loaded on first autocomplete interaction).
    _tagDb: null,             // { tags: [], aliases: {}, byName: Map, names: [], version }
    _tagDbLoading: null,      // in-flight promise (dedupe concurrent loads)
    _activeTagPlatform: 'default',
    _tagDropdownOpenFor: null,   // platform key the dropdown is currently open for
    _tagDropdownIndex: 0,
    _tagDropdownResults: [],     // last rendered result set

    // Phase 3b: e621 lookup fallback — surfaced when local results are sparse.
    _e621LookupCache: new Map(),     // lowercase query → array<{name,category,post_count}>
    _e621LookupPending: null,        // in-flight {query, promise} to dedupe concurrent fetches
    _e621LookupResults: [],          // last rendered e621 result set (parallel to _tagDropdownResults)
    _e621LookupDebounce: null,       // setTimeout handle
    _E621_LOOKUP_MIN_QUERY: 3,       // min chars before triggering lookup
    _E621_LOOKUP_LOCAL_THRESHOLD: 5, // only trigger when local matches < this
    _E621_LOOKUP_DEBOUNCE_MS: 300,

    // Phase 4b: tag autocomplete context — the dropdown portal is a single
    // element shared between story-level tags and per-chapter tags. Context
    // tracks where the dropdown is currently writing to.
    //   shape: { scope: 'story'|'chapter', platform: 'default'|..., chapterIdx: null|number }
    _tagDropdownContext: null,
    // ID of the input the portal is currently anchored to (for positioning).
    _currentTagInputId: 'metadata-tag-input',
    // Active sub-tab per chapter expanded detail (remembers choice per row).
    _chapterTagPlatformByIdx: {},

    // Phase 3a+: expanded tag browser modal state
    _tagBrowserOpen: false,
    _tagBrowserQuery: '',
    _tagBrowserFilters: new Set(),
    _tagBrowserPage: 1,           // page size = 100 results
    _TAG_BROWSER_PAGE_SIZE: 100,
    _TAG_BROWSER_CATEGORIES: ['physical', 'acts', 'kink', 'meta', 'image', 'user'],

    // Phase 4: Chapters
    _chapterData: null,           // { chapters, drift } from GET /chapters
    _chaptersLoading: false,
    _chaptersLoaded: false,       // flip true once we have data at least once
    _expandedChapter: null,       // chapter index currently expanded (or null)
    _activeChapterTagPlatform: 'default', // sub-tab within chapter detail
    _CHAPTER_TAG_PLATFORMS: ['default', 'sofurry', 'inkbunny', 'wattpad'],

    // Phase 5: Cover + Raw JSON
    _coverFilename: null,         // current metadata.images.cover
    _coverBustKey: 0,             // cache-bust counter for preview img
    _coverUploading: false,
    _rawJsonEditMode: false,

    // Platform tag caps (∞ = no cap on count). FA's real cap is on the
    // joined keyword string (500 chars), not the count — _renderTagTabBody
    // shows that as a separate counter when the FA tab is active.
    TAG_LIMITS: {
        sofurry: 97,
        wattpad: 24,
        inkbunny: Infinity,
        furaffinity: Infinity,
        weasyl: Infinity,
        ao3: Infinity,
        squidgeworld: Infinity,
        default: Infinity,
    },

    // FA's real validator (posting/platforms/furaffinity.py:227-228) caps
    // the space-joined keyword string at 500 chars, not the tag count.
    // Surface that in the UI alongside the count so the user knows when
    // they need to trim before posting (e.g. Tombstone's 91-tag default
    // list serialises to 814 chars and gets rejected).
    FA_TAG_STRING_MAX: 500,

    // Canonical ratings (must match backend whitelist)
    RATINGS: [
        'Not Rated',
        'General Audiences',
        'Teen And Up Audiences',
        'Mature',
        'Explicit',
    ],

    // AO3 archive warnings (canonical list)
    WARNINGS: [
        'No Archive Warnings Apply',
        'Choose Not To Use Archive Warnings',
        'Graphic Depictions Of Violence',
        'Major Character Death',
        'Rape/Non-Con',
        'Underage Sex',
    ],

    // AO3 categories
    CATEGORIES: ['F/F', 'F/M', 'Gen', 'M/M', 'Multi', 'Other'],

    // Platform keys (order controls render)
    PLATFORMS: ['sofurry', 'inkbunny', 'squidgeworld', 'ao3', 'furaffinity', 'wattpad'],

    // Platforms that show as tabs in the Tags section. Order matches
    // PLATFORMS for the underlying posters with Default leading; new tabs
    // (FA / Weasyl / AO3 / SQW) inherit from default on first load and
    // become independent override lists once the user edits them.
    TAG_PLATFORMS: ['default', 'sofurry', 'inkbunny', 'furaffinity', 'weasyl', 'ao3', 'squidgeworld', 'wattpad'],

    // Platforms that receive cascaded tags when the user adds/removes a
    // tag from the Default tab. Broader than TAG_PLATFORMS so AO3, SQW,
    // WS, FA, DA, IK also pick up new tags even though they don't have
    // their own UI tabs. Bluesky is excluded — it uses hashtag-style
    // freeform tags that don't map from canonical underscored tags.
    TAG_CASCADE_PLATFORMS: [
        'sofurry', 'inkbunny', 'wattpad',
        'ao3', 'squidgeworld', 'weasyl', 'furaffinity',
        'deviantart', 'itaku',
    ],

    // Human-readable platform names
    PLATFORM_LABELS: {
        sofurry: 'SoFurry',
        inkbunny: 'Inkbunny',
        squidgeworld: 'SquidgeWorld',
        ao3: 'AO3',
        furaffinity: 'FurAffinity',
        weasyl: 'Weasyl',
        wattpad: 'Wattpad',
        default: 'Default',
    },

    // Character limits (soft — warns in counter, no hard validation)
    DESC_MAX: 500,
    SUMMARY_MAX: 2000,
    ANNOUNCEMENT_MAX: 300,

    // ---------------------------------------------------------------------
    // Public entry point
    // ---------------------------------------------------------------------

    async toggle() {
        if (this.isOpen) {
            this.close();
            return;
        }
        if (!Editor.storyName) {
            alert('No story loaded.');
            return;
        }
        this.storyName = Editor.storyName;

        this._mountDrawer();
        this.isOpen = true;

        try {
            await this._loadMetadata();
            this._renderForm();
            this._initFormBindings();
        } catch (err) {
            this._renderError(err.message || String(err));
        }
    },

    close() {
        if (!this.isOpen) return;
        if (this._isDirty()) {
            if (!confirm('Discard unsaved metadata changes?')) return;
        }
        const root = document.getElementById('metadata-drawer-root');
        if (root) {
            // Trigger slide-out then remove
            const drawer = root.querySelector('.metadata-drawer');
            if (drawer) drawer.classList.remove('open');
            setTimeout(() => root.remove(), 200);
        }
        this.isOpen = false;
        this.metadata = null;
        this.initialMetadata = null;
        this.lastMtime = 0;
        this.storyName = null;
    },

    // ---------------------------------------------------------------------
    // Mount / unmount drawer shell
    // ---------------------------------------------------------------------

    _mountDrawer() {
        // Remove any stale instance first
        const existing = document.getElementById('metadata-drawer-root');
        if (existing) existing.remove();

        const root = document.createElement('div');
        root.id = 'metadata-drawer-root';
        root.innerHTML = `
            <div class="metadata-drawer-backdrop" id="metadata-drawer-backdrop"></div>
            <aside class="metadata-drawer" id="metadata-drawer" role="dialog" aria-label="Story metadata">
                <div class="metadata-drawer-header">
                    <div class="metadata-drawer-title">
                        <span>Metadata</span>
                        <span class="metadata-drawer-subtitle" id="metadata-drawer-subtitle"></span>
                    </div>
                    <div class="metadata-drawer-actions">
                        <span class="metadata-drawer-status" id="metadata-drawer-status"></span>
                        <button class="btn btn-sm" id="metadata-save-btn">Save</button>
                        <button class="btn btn-sm btn-outline" id="metadata-close-btn" title="Close">&times;</button>
                    </div>
                </div>
                <div class="metadata-drawer-body" id="metadata-drawer-body">
                    <div class="metadata-loading">Loading metadata...</div>
                </div>
            </aside>
        `;
        document.body.appendChild(root);

        // Trigger slide-in on next frame (so transition plays)
        requestAnimationFrame(() => {
            document.getElementById('metadata-drawer')?.classList.add('open');
        });

        // Wire up shell buttons once (form bindings happen after _renderForm)
        const mountedAt = Date.now();
        document.getElementById('metadata-close-btn')?.addEventListener('click', () => this.close());
        document.getElementById('metadata-drawer-backdrop')?.addEventListener('click', () => {
            // Mobile fires a synthetic click ~300ms after touchend on whatever
            // element is under the finger at that moment. Since we mount the
            // backdrop synchronously inside the button's click handler, the
            // backdrop is now under the user's finger and catches the
            // synthetic click — closing the drawer the instant it opens.
            // Ignore backdrop clicks within the synthetic-click window.
            if (Date.now() - mountedAt < 400) return;
            this.close();
        });
        document.getElementById('metadata-save-btn')?.addEventListener('click', () => this.save());
    },

    _renderError(msg) {
        const body = document.getElementById('metadata-drawer-body');
        if (body) {
            body.innerHTML = `<div class="metadata-error-banner">Failed to load metadata: ${this._escape(msg)}</div>`;
        }
    },

    // ---------------------------------------------------------------------
    // Load metadata from backend
    // ---------------------------------------------------------------------

    async _loadMetadata() {
        const resp = await fetch(`/api/editor/stories/${encodeURIComponent(this.storyName)}/metadata`);
        if (!resp.ok) {
            const txt = await resp.text();
            throw new Error(txt || `HTTP ${resp.status}`);
        }
        const data = await resp.json();
        this.metadata = data.metadata || {};
        this.lastMtime = data.last_modified || 0;
        // Snapshot initial state for dirty tracking (deep clone via JSON)
        this.initialMetadata = JSON.parse(JSON.stringify(this.metadata));
        // Drawer subtitle shows story title for context
        const sub = document.getElementById('metadata-drawer-subtitle');
        if (sub) sub.textContent = this.metadata.title || this.storyName;
    },

    // ---------------------------------------------------------------------
    // Form rendering
    // ---------------------------------------------------------------------

    _renderForm() {
        const body = document.getElementById('metadata-drawer-body');
        if (!body) return;

        // Normalise Phase 2 fields on the live metadata so later reads/writes
        // operate on arrays/objects instead of undefined.
        this._normaliseMetadata();

        const md = this.metadata || {};
        const ratingOptions = ['', ...this.RATINGS]
            .map(r => {
                const selected = (md.rating || '').toString().toLowerCase() === r.toLowerCase() ? ' selected' : '';
                const label = r || '(unset)';
                return `<option value="${this._escape(r)}"${selected}>${this._escape(label)}</option>`;
            })
            .join('');

        const descVal = md.description || '';
        const summaryVal = md.summary || '';
        const subtitleVal = md.subtitle || '';
        const dedicationVal = md.dedication || '';

        body.innerHTML = `
            <section class="metadata-section" data-section="info" data-expanded="true">
                <button type="button" class="metadata-section-header" data-section-toggle="info">
                    <span class="metadata-section-chevron">&#9660;</span>
                    <span>Story Info</span>
                </button>
                <div class="metadata-section-body">
                    <div class="metadata-field">
                        <label for="meta-title">Title <span class="metadata-required">*</span></label>
                        <input type="text" id="meta-title" data-field="title" value="${this._escape(md.title || '')}" autocomplete="off" />
                        <div class="metadata-error" id="meta-error-title"></div>
                    </div>
                    <div class="metadata-field">
                        <label for="meta-author">Author</label>
                        <input type="text" id="meta-author" data-field="author" value="${this._escape(md.author || '')}" autocomplete="off" />
                    </div>
                    <div class="metadata-field">
                        <label for="meta-subtitle">Subtitle / tagline</label>
                        <input type="text" id="meta-subtitle" data-field="subtitle" value="${this._escape(subtitleVal)}" autocomplete="off" placeholder="Optional — appears under the title on the EPUB title page" />
                    </div>
                    <div class="metadata-field">
                        <label for="meta-fandom">Fandom</label>
                        <input type="text" id="meta-fandom" data-field="fandom" value="${this._escape(md.fandom || '')}" autocomplete="off" />
                    </div>
                    <div class="metadata-field metadata-field-row">
                        <div class="metadata-field-col metadata-field-col--grow">
                            <label for="meta-series">Series</label>
                            <input type="text" id="meta-series" data-field="series" value="${this._escape(md.series || '')}" autocomplete="off" placeholder="Optional — group related works, e.g. &quot;Velvet &amp; Vice&quot;" />
                        </div>
                        <div class="metadata-field-col metadata-field-col--narrow">
                            <label for="meta-series-index">No. in series</label>
                            <input type="number" id="meta-series-index" data-field="series_index" value="${this._escape(String(md.series_index || ''))}" min="0" step="1" autocomplete="off" placeholder="#" />
                        </div>
                    </div>
                    <div class="metadata-field">
                        <label for="meta-rating">Rating</label>
                        <select id="meta-rating" data-field="rating">
                            ${ratingOptions}
                        </select>
                        <div class="metadata-error" id="meta-error-rating"></div>
                    </div>
                </div>
            </section>

            <section class="metadata-section" data-section="desc" data-expanded="false">
                <button type="button" class="metadata-section-header" data-section-toggle="desc">
                    <span class="metadata-section-chevron">&#9654;</span>
                    <span>Description &amp; Summary</span>
                </button>
                <div class="metadata-section-body">
                    <div class="metadata-field">
                        <label for="meta-description">Description <span class="metadata-char-counter" id="meta-desc-counter"></span></label>
                        <textarea id="meta-description" data-field="description" rows="3">${this._escape(descVal)}</textarea>
                    </div>
                    <div class="metadata-field">
                        <label for="meta-summary">Summary <span class="metadata-char-counter" id="meta-summary-counter"></span></label>
                        <textarea id="meta-summary" data-field="summary" rows="8">${this._escape(summaryVal)}</textarea>
                    </div>
                    <div class="metadata-field">
                        <label for="meta-dedication">Dedication</label>
                        <textarea id="meta-dedication" data-field="dedication" rows="3" placeholder="Optional — appears as its own page in the EPUB before the author's note">${this._escape(dedicationVal)}</textarea>
                    </div>
                    ${this._renderPerPlatformDescs()}
                </div>
            </section>

            ${this._renderClassificationsSection()}
            ${this._renderPlatformTagsSection()}
            ${this._renderPlatformTogglesSection()}
            ${this._renderChaptersSection()}
            ${this._renderCoverSection()}
            ${this._renderRawJsonSection()}
        `;

        this._updateCharCounter('meta-description', 'meta-desc-counter', this.DESC_MAX);
        this._updateCharCounter('meta-summary', 'meta-summary-counter', this.SUMMARY_MAX);
        this._updateCharCounter('meta-desc-short', 'meta-desc-short-counter', this.DESC_MAX);
        this._updateCharCounter('meta-desc-announcement', 'meta-desc-announcement-counter', this.ANNOUNCEMENT_MAX);
    },

    /**
     * Ensure all Phase 2 metadata fields exist as the right JS type so
     * subsequent renderers/binders can assume arrays + objects.
     */
    _normaliseMetadata() {
        const md = this.metadata = this.metadata || {};

        if (!Array.isArray(md.warnings)) {
            md.warnings = [];
        }

        // Legacy: `category` (string) → `categories` (array)
        if (!Array.isArray(md.categories)) {
            if (typeof md.category === 'string' && md.category.trim()) {
                md.categories = [md.category.trim()];
            } else {
                md.categories = [];
            }
        }

        if (!Array.isArray(md.characters)) md.characters = [];
        if (!Array.isArray(md.relationships)) md.relationships = [];

        if (!md.tags || typeof md.tags !== 'object' || Array.isArray(md.tags)) {
            md.tags = {};
        }
        this.TAG_PLATFORMS.forEach(p => {
            if (!Array.isArray(md.tags[p])) md.tags[p] = [];
        });

        if (!md.platforms || typeof md.platforms !== 'object' || Array.isArray(md.platforms)) {
            md.platforms = {};
        }
        this.PLATFORMS.forEach(p => {
            if (typeof md.platforms[p] !== 'boolean') md.platforms[p] = !!md.platforms[p];
        });

        // Phase 4: chapter_info — list of { index, title, description, tags, words }
        if (!Array.isArray(md.chapter_info)) md.chapter_info = [];

        // Per-platform description overrides (short, announcement)
        if (!md.descriptions || typeof md.descriptions !== 'object' || Array.isArray(md.descriptions)) {
            md.descriptions = {};
        }

        // Phase 5: images map (cover filename lives at images.cover)
        if (!md.images || typeof md.images !== 'object' || Array.isArray(md.images)) {
            md.images = {};
        }
        this._coverFilename = (typeof md.images.cover === 'string' && md.images.cover.trim()) ? md.images.cover.trim() : null;
    },

    // ---------------------------------------------------------------------
    // Per-platform description overrides (collapsible sub-section)
    // ---------------------------------------------------------------------

    _renderPerPlatformDescs() {
        const descs = (this.metadata && this.metadata.descriptions) || {};
        const shortVal = descs.short || '';
        const announcementVal = descs.announcement || '';
        return `
            <details class="metadata-desc-details">
                <summary class="metadata-desc-toggle">Per-platform descriptions (optional overrides)</summary>
                <div class="metadata-desc-tabs">
                    <button type="button" data-desc-tab="short" class="metadata-desc-tab active">Short (IB/SF)</button>
                    <button type="button" data-desc-tab="announcement" class="metadata-desc-tab">Announcement (Bsky)</button>
                </div>
                <div class="metadata-desc-pane" data-desc-pane="short">
                    <div class="metadata-field">
                        <label for="meta-desc-short">Short description <span class="metadata-char-counter" id="meta-desc-short-counter"></span></label>
                        <textarea id="meta-desc-short" rows="3" placeholder="1-2 sentences for listing pages (IB, SF, FA, WS)...">${this._escape(shortVal)}</textarea>
                        <div class="metadata-hint">Overrides the default description for Inkbunny, SoFurry, FurAffinity, and Weasyl.</div>
                    </div>
                </div>
                <div class="metadata-desc-pane" data-desc-pane="announcement" style="display:none">
                    <div class="metadata-field">
                        <label for="meta-desc-announcement">Announcement <span class="metadata-char-counter" id="meta-desc-announcement-counter"></span></label>
                        <textarea id="meta-desc-announcement" rows="2" placeholder="Quick announcement, 300 chars max..." maxlength="300">${this._escape(announcementVal)}</textarea>
                        <div class="metadata-hint">Short text for Bluesky posts. Falls back to truncated description if empty.</div>
                    </div>
                </div>
            </details>`;
    },

    // ---------------------------------------------------------------------
    // Section 3: Classifications
    // ---------------------------------------------------------------------

    _renderClassificationsSection() {
        const md = this.metadata;

        const warningsHtml = this.WARNINGS.map((w, i) => {
            const checked = md.warnings.includes(w) ? ' checked' : '';
            const id = `meta-warning-${i}`;
            return `
                <label class="metadata-checkbox" for="${id}">
                    <input type="checkbox" id="${id}" data-classification="warning" value="${this._escape(w)}"${checked} />
                    <span>${this._escape(w)}</span>
                </label>
            `;
        }).join('');

        const categoriesHtml = this.CATEGORIES.map((c, i) => {
            const checked = md.categories.includes(c) ? ' checked' : '';
            const id = `meta-category-${i}`;
            return `
                <label class="metadata-checkbox" for="${id}">
                    <input type="checkbox" id="${id}" data-classification="category" value="${this._escape(c)}"${checked} />
                    <span>${this._escape(c)}</span>
                </label>
            `;
        }).join('');

        return `
            <section class="metadata-section" data-section="classifications" data-expanded="false">
                <button type="button" class="metadata-section-header" data-section-toggle="classifications">
                    <span class="metadata-section-chevron">&#9654;</span>
                    <span>Classifications</span>
                </button>
                <div class="metadata-section-body">
                    <div class="metadata-field">
                        <label>Archive Warnings <span class="metadata-required">*</span></label>
                        <div class="metadata-checkbox-list" id="meta-warnings-list">
                            ${warningsHtml}
                        </div>
                        <div class="metadata-error" id="meta-error-warnings"></div>
                    </div>
                    <div class="metadata-field">
                        <label>Categories</label>
                        <div class="metadata-checkbox-row" id="meta-categories-list">
                            ${categoriesHtml}
                        </div>
                    </div>
                    <div class="metadata-field">
                        <label for="meta-characters-input">Characters</label>
                        ${this._renderPillInput('characters', md.characters, 'Type a character and press Enter')}
                    </div>
                    <div class="metadata-field">
                        <label for="meta-relationships-input">Relationships</label>
                        ${this._renderPillInput('relationships', md.relationships, 'e.g. Alice/Bob, Alice & Bob')}
                    </div>
                </div>
            </section>
        `;
    },

    // ---------------------------------------------------------------------
    // Section 4: Per-Platform Tags (Phase 3a — autocomplete)
    //
    // UI: one section containing a tab strip (Default/SoFurry/Wattpad/Inkbunny);
    // active tab shows a pill list + autocomplete input + a footer counter.
    // Pills write back to `metadata.tags.<platform>` so the standard dirty
    // check picks up changes with no extra wiring.
    // ---------------------------------------------------------------------

    _renderPlatformTagsSection() {
        const tabs = this.TAG_PLATFORMS.map(p => {
            const active = p === this._activeTagPlatform ? ' metadata-tag-tab-active' : '';
            return `<button type="button" class="metadata-tag-tab${active}" data-tag-tab="${this._escape(p)}">${this._escape(this.PLATFORM_LABELS[p] || p)}</button>`;
        }).join('');

        return `
            <section class="metadata-section" data-section="platform-tags" data-expanded="false">
                <button type="button" class="metadata-section-header" data-section-toggle="platform-tags">
                    <span class="metadata-section-chevron">&#9654;</span>
                    <span>Per-Platform Tags</span>
                </button>
                <div class="metadata-section-body">
                    <div class="metadata-tag-tabs" role="tablist">${tabs}
                        <button type="button" class="metadata-tag-fix-spaces" id="metadata-tag-fix-spaces" title="Replace spaces with underscores in Default tags">Fix spaces</button>
                        <button type="button" class="metadata-tag-fix-spaces" id="metadata-tag-sort-alpha" title="Sort tags alphabetically on all platforms">Sort A-Z</button>
                    </div>
                    <div class="metadata-tag-tab-body" id="metadata-tag-tab-body">
                        ${this._renderTagTabBody(this._activeTagPlatform)}
                    </div>
                </div>
            </section>
        `;
    },

    _renderTagTabBody(platform) {
        const md = this.metadata;
        const tags = md.tags[platform] || [];
        const aliases = this._tagDb ? this._tagDb.aliases : {};
        const byName = this._tagDb ? this._tagDb.byName : null;

        // Backfill helper for existing stories whose story.json predates the
        // new tabs (e.g. Tombstone has no `furaffinity` / `ao3` namespace).
        // New stories don't need this — TAG_CASCADE_PLATFORMS keeps every
        // platform in sync as the user edits Default.
        const isEmpty = tags.length === 0;
        const defaultHasTags = (md.tags.default || []).length > 0;
        const showPopulate = isEmpty && defaultHasTags && platform !== 'default';

        const pills = tags.map((t, i) => {
            const inDb = byName ? byName.has(t) : false;
            const aliasTarget = (!inDb && aliases[t]) ? aliases[t] : null;
            let cls = 'metadata-tag-pill';
            if (byName && !inDb && !aliasTarget) cls += ' metadata-tag-pill-unknown';
            const aliasNote = aliasTarget ? `<span class="metadata-tag-pill-alias">&rarr; ${this._escape(aliasTarget)}</span>` : '';
            return `
                <span class="${cls}" data-tag-pill-index="${i}">
                    <span class="metadata-tag-pill-text">${this._escape(t)}</span>
                    ${aliasNote}
                    <button type="button" class="metadata-tag-pill-remove" data-tag-remove="${this._escape(platform)}" data-index="${i}" aria-label="Remove tag">&times;</button>
                </span>
            `;
        }).join('');

        const limit = this.TAG_LIMITS[platform];
        const limitLabel = (limit === Infinity) ? '&infin;' : limit;
        const overLimit = (limit !== Infinity) && tags.length > limit;

        // FA's real cap is on the space-joined string, not the count.
        // Show a second counter when the FA tab is active so the user
        // can see they're heading for the validator's 500-char rejection
        // before they hit Save.
        let extraCounter = '';
        if (platform === 'furaffinity') {
            const joinedLen = tags.join(' ').length;
            const charOver = joinedLen > this.FA_TAG_STRING_MAX;
            extraCounter = `
                <span class="metadata-tag-count-sep">&middot;</span>
                <span class="${charOver ? 'metadata-tag-count-over' : ''}"
                      title="FA validator rejects keyword strings over 500 chars">${joinedLen} / ${this.FA_TAG_STRING_MAX} chars</span>`;
        }

        const populateBtn = showPopulate
            ? `<button type="button" class="btn btn-sm btn-outline metadata-tag-populate"
                       id="metadata-tag-populate-btn"
                       data-populate-platform="${this._escape(platform)}"
                       title="Copy every Default tag into this platform's list (transformed for ${this._escape(this.PLATFORM_LABELS[platform] || platform)})">
                Populate from Default (${(md.tags.default || []).length})
              </button>`
            : '';

        return `
            <div class="metadata-tag-pills" id="metadata-tag-pills-${this._escape(platform)}">${pills}</div>
            ${populateBtn}
            <div class="metadata-tag-input-wrap">
                <input type="text"
                       class="metadata-tag-input"
                       id="metadata-tag-input"
                       data-tag-platform-input="${this._escape(platform)}"
                       placeholder="Add tag..."
                       autocomplete="off" />
                <div class="metadata-tag-dropdown" id="metadata-tag-dropdown" hidden></div>
            </div>
            <div class="metadata-tag-count ${overLimit ? 'metadata-tag-count-over' : ''}">
                <span id="metadata-tag-count-text">${tags.length} tags</span>
                <span class="metadata-tag-count-sep">&middot;</span>
                <span>Platform max: ${limitLabel}</span>
                ${extraCounter}
            </div>
        `;
    },

    _rerenderTagTabBody() {
        const host = document.getElementById('metadata-tag-tab-body');
        if (!host) return;
        host.innerHTML = this._renderTagTabBody(this._activeTagPlatform);
        this._bindTagTabBodyEvents();
    },

    _updateTagTabs() {
        document.querySelectorAll('[data-tag-tab]').forEach(b => {
            const p = b.getAttribute('data-tag-tab');
            b.classList.toggle('metadata-tag-tab-active', p === this._activeTagPlatform);
        });
    },

    // ---- Tag DB loading (lazy, cached in sessionStorage by version hash) ----

    async _loadTagDb() {
        if (this._tagDb) return this._tagDb;
        if (this._tagDbLoading) return this._tagDbLoading;

        this._tagDbLoading = (async () => {
            // Try sessionStorage cache first, but still fetch to check version.
            // We could optimise by sending If-None-Match, but the payload is
            // tiny gzipped and parsed once per session — keep it simple.
            try {
                const cachedRaw = sessionStorage.getItem('pawpoller_tag_db_v1');
                if (cachedRaw) {
                    const cached = JSON.parse(cachedRaw);
                    if (cached && cached.version && cached.tags) {
                        this._tagDb = this._indexTagDb(cached);
                        // Fire off a background refresh so stale versions self-heal
                        this._refreshTagDbBackground();
                        return this._tagDb;
                    }
                }
            } catch (_) { /* corrupted cache — ignore */ }

            const resp = await fetch('/api/editor/tags');
            if (!resp.ok) {
                const t = await resp.text();
                throw new Error(`Tag DB load failed: ${t || resp.status}`);
            }
            const data = await resp.json();
            try { sessionStorage.setItem('pawpoller_tag_db_v1', JSON.stringify(data)); } catch (_) { /* quota */ }
            this._tagDb = this._indexTagDb(data);
            return this._tagDb;
        })();

        try {
            return await this._tagDbLoading;
        } finally {
            this._tagDbLoading = null;
        }
    },

    async _refreshTagDbBackground() {
        try {
            const resp = await fetch('/api/editor/tags');
            if (!resp.ok) return;
            const data = await resp.json();
            if (this._tagDb && data.version === this._tagDb.version) return;
            try { sessionStorage.setItem('pawpoller_tag_db_v1', JSON.stringify(data)); } catch (_) {}
            this._tagDb = this._indexTagDb(data);
            // Refresh current view so any unknown-tag styling gets re-evaluated
            if (this.isOpen) this._rerenderTagTabBody();
        } catch (_) { /* silent */ }
    },

    _indexTagDb(data) {
        const byName = new Map();
        for (const t of data.tags) byName.set(t.name, t);
        // Pre-lowercase names for cheap case-insensitive filtering
        const names = data.tags.map(t => ({
            name: t.name,
            lower: t.name.toLowerCase(),
            tag: t,
        }));
        return {
            tags: data.tags,
            aliases: data.aliases || {},
            version: data.version,
            byName,
            names,
        };
    },

    // ---- Dropdown rendering + filtering ----

    _filterTagResults(query) {
        if (!this._tagDb) return [];
        const q = (query || '').toLowerCase().trim();
        if (!q) return [];

        const { names, aliases, byName } = this._tagDb;
        const exact = [];
        const prefix = [];
        const substring = [];

        for (const entry of names) {
            if (entry.lower === q) exact.push({ kind: 'tag', tag: entry.tag });
            else if (entry.lower.startsWith(q)) prefix.push({ kind: 'tag', tag: entry.tag });
            else if (entry.lower.includes(q)) substring.push({ kind: 'tag', tag: entry.tag });
            if (exact.length + prefix.length + substring.length >= 120) break;
        }

        // Alias matches — show canonical tag with "(alias)" badge
        const aliasResults = [];
        const qAliasExact = aliases[q];
        if (qAliasExact && byName.has(qAliasExact)) {
            aliasResults.push({ kind: 'alias', from: q, tag: byName.get(qAliasExact) });
        }
        // Also substring match on aliases (cheap — ~23K entries)
        let aliasCount = 0;
        for (const [aliasKey, canonical] of Object.entries(aliases)) {
            if (aliasCount >= 10) break;
            if (aliasKey === q) continue; // already added above
            if (aliasKey.toLowerCase().includes(q) && byName.has(canonical)) {
                aliasResults.push({ kind: 'alias', from: aliasKey, tag: byName.get(canonical) });
                aliasCount++;
            }
        }

        const combined = [...exact, ...aliasResults, ...prefix, ...substring];

        // Dedup by canonical tag name, keeping first occurrence (preserves priority)
        const seen = new Set();
        const out = [];
        for (const r of combined) {
            if (seen.has(r.tag.name)) continue;
            seen.add(r.tag.name);
            out.push(r);
            if (out.length >= 30) break;
        }
        return out;
    },

    _ensureDropdownPortal() {
        // Render the dropdown as a body-level portal so it escapes the
        // metadata-section-body overflow:hidden + drawer transform.
        let dd = document.getElementById('metadata-tag-dropdown-portal');
        if (!dd) {
            dd = document.createElement('div');
            dd.id = 'metadata-tag-dropdown-portal';
            dd.className = 'metadata-tag-dropdown-portal';
            dd.hidden = true;
            document.body.appendChild(dd);
        }
        return dd;
    },

    _positionDropdownPortal() {
        const dd = document.getElementById('metadata-tag-dropdown-portal');
        const input = document.getElementById(this._currentTagInputId || 'metadata-tag-input');
        if (!dd || !input) return;
        const rect = input.getBoundingClientRect();
        const viewportH = window.innerHeight;
        const spaceBelow = viewportH - rect.bottom - 10;
        const spaceAbove = rect.top - 10;
        const width = Math.max(380, rect.width);
        // Prefer below; flip up if cramped
        if (spaceBelow < 200 && spaceAbove > spaceBelow) {
            dd.style.top = '';
            dd.style.bottom = (viewportH - rect.top + 4) + 'px';
            dd.style.maxHeight = Math.min(480, spaceAbove) + 'px';
        } else {
            dd.style.bottom = '';
            dd.style.top = (rect.bottom + 4) + 'px';
            dd.style.maxHeight = Math.min(480, spaceBelow) + 'px';
        }
        dd.style.left = rect.left + 'px';
        dd.style.width = width + 'px';
    },

    _renderDropdown(results, query) {
        // Use body-level portal instead of in-tree dropdown to escape
        // overflow:hidden on parent sections.
        const dd = this._ensureDropdownPortal();
        if (!dd) return;
        this._tagDropdownResults = results;
        const footer = `
            <div class="metadata-tag-dropdown-footer">
                <button type="button" class="metadata-tag-browse-btn" data-action="open-tag-browser">
                    &#128269; Browse all matches in expanded view &rarr;
                </button>
            </div>
        `;

        // Phase 3b: e621 suggestions block (appended below local results).
        const e621Block = this._renderE621SuggestionsBlock(query);

        if (!results.length) {
            const q = (query || '').trim();
            if (q) {
                dd.innerHTML = `<div class="metadata-tag-result-empty">No matches &mdash; Press Enter to add "<span>${this._escape(q)}</span>" anyway</div>${e621Block}${footer}`;
                dd.hidden = false;
            } else {
                dd.innerHTML = footer;
                dd.hidden = false;
            }
            this._positionDropdownPortal();
            return;
        }
        const rows = results.map((r, i) => {
            const t = r.tag;
            const active = i === this._tagDropdownIndex ? ' metadata-tag-result-active' : '';
            const aliasBadge = r.kind === 'alias'
                ? `<span class="metadata-tag-result-alias">alias of &ldquo;${this._escape(r.from)}&rdquo;</span>` : '';
            const desc = t.desc ? `<div class="metadata-tag-result-desc">${this._escape(this._truncate(t.desc, 110))}</div>` : '';
            return `
                <div class="metadata-tag-result${active}" data-tag-result-index="${i}">
                    <div class="metadata-tag-result-row">
                        <span class="metadata-tag-result-name">${this._escape(t.name)}</span>
                        <span class="metadata-tag-result-cat metadata-tag-cat-${this._escape(t.category)}">${this._escape(t.category)}</span>
                        ${aliasBadge}
                    </div>
                    ${desc}
                </div>
            `;
        }).join('');
        dd.innerHTML = rows + e621Block + footer;
        dd.hidden = false;
        this._positionDropdownPortal();
    },

    // ----------------------------------------------------------------
    // Phase 3b: e621 lookup fallback — rendered below local matches.
    // ----------------------------------------------------------------

    /**
     * Human label for the raw e621 category integer. 0=general, 3=copyright,
     * 5=species, 7=meta, 8=lore. We only surface these five (filter drops
     * 1/2/4).
     */
    _e621CategoryLabel(catInt) {
        switch (catInt) {
            case 0: return 'general';
            case 3: return 'copyright';
            case 5: return 'species';
            case 7: return 'meta';
            case 8: return 'lore';
            default: return 'e621';
        }
    },

    /**
     * Map an e621 category int to the most natural local DB bucket for the
     * primary "+ Library" shortcut. Returns a `target` accepted by
     * /api/editor/tags/add.
     */
    _suggestedTargetForE621Cat(catInt) {
        switch (catInt) {
            case 5: return 'physical';  // species → body/physical
            case 0: return 'user';      // general → catch-all
            case 3: return 'meta';      // copyright → meta
            case 7: return 'image';     // meta → image-ish
            case 8: return 'kink';      // lore → kink (lore is often kink-adjacent)
            default: return 'user';
        }
    },

    _targetLabel(target) {
        const map = {
            physical: 'Physical',
            acts: 'Acts',
            kink: 'Kink',
            meta: 'Meta',
            image: 'Image',
            user: 'User',
        };
        return map[target] || target;
    },

    /**
     * Render the e621 suggestions block HTML. Returns "" when there's nothing
     * to show (either the cache has no entry for this query, or the entry is
     * empty). The block sits between the local-result rows and the footer.
     */
    _renderE621SuggestionsBlock(query) {
        const q = (query || '').trim().toLowerCase();
        if (!q || q.length < this._E621_LOOKUP_MIN_QUERY) {
            this._e621LookupResults = [];
            return '';
        }
        // Loading state
        if (this._e621LookupPending && this._e621LookupPending.query === q) {
            return `
                <div class="metadata-tag-result-divider">
                    &#128218; Searching e621 for more suggestions&hellip;
                </div>
            `;
        }
        const matches = this._e621LookupCache.get(q);
        if (!matches || !matches.length) {
            this._e621LookupResults = [];
            // Only show "no e621 matches" if cache definitely returned (not pending)
            if (this._e621LookupCache.has(q)) {
                return `<div class="metadata-tag-result-divider">&#128218; No e621 matches for "${this._escape(q)}"</div>`;
            }
            return '';
        }
        this._e621LookupResults = matches;
        const rows = matches.map((m, i) => {
            const catLabel = this._e621CategoryLabel(m.category);
            const suggestedTarget = this._suggestedTargetForE621Cat(m.category);
            const count = (m.post_count || 0).toLocaleString();
            // Primary button uses the suggested target. "User" is the
            // neutral generic bucket for general cat 0.
            const primaryLabel = `+ ${this._targetLabel(suggestedTarget)}`;
            return `
                <div class="metadata-tag-result-e621" data-e621-index="${i}" data-e621-name="${this._escape(m.name)}" data-e621-cat="${m.category}">
                    <div class="metadata-tag-result-row">
                        <span class="metadata-tag-result-name">${this._escape(m.name)}</span>
                        <span class="metadata-tag-result-cat metadata-tag-cat-e621">e621 ${this._escape(catLabel)}</span>
                        <span class="metadata-tag-result-count">${count} posts</span>
                    </div>
                    <div class="metadata-tag-result-actions">
                        <button type="button" class="metadata-tag-add-library-btn"
                                data-action="add-to-library"
                                data-e621-name="${this._escape(m.name)}"
                                data-target="${this._escape(suggestedTarget)}">
                            ${this._escape(primaryLabel)}
                        </button>
                        <div class="metadata-tag-target-menu">
                            <button type="button" class="metadata-tag-target-caret"
                                    data-action="toggle-target-menu"
                                    data-e621-index="${i}"
                                    aria-label="Choose library target">&#9662;</button>
                            <div class="metadata-tag-target-menu-list" data-target-menu-for="${i}" hidden>
                                ${['physical', 'acts', 'kink', 'meta', 'image', 'user'].map(t => `
                                    <button type="button" class="metadata-tag-target-menu-item"
                                            data-action="add-to-library"
                                            data-e621-name="${this._escape(m.name)}"
                                            data-target="${this._escape(t)}">
                                        + ${this._escape(this._targetLabel(t))}
                                    </button>
                                `).join('')}
                            </div>
                        </div>
                        <button type="button" class="metadata-tag-use-once-btn"
                                data-action="use-once"
                                data-e621-name="${this._escape(m.name)}">
                            Use once
                        </button>
                    </div>
                </div>
            `;
        }).join('');
        return `
            <div class="metadata-tag-result-divider">
                &#128218; e621 suggestions (not in your library)
            </div>
            ${rows}
        `;
    },

    /**
     * Schedule an e621 lookup fetch for `query` against the current input.
     * Debounced — repeated keystrokes within the window collapse into one
     * fetch. Called from _openDropdownFor after local filtering.
     */
    _maybeTriggerE621Lookup(query, localResultCount) {
        const q = (query || '').trim().toLowerCase();
        // Guard: skip if query is too short OR local already has enough matches.
        if (q.length < this._E621_LOOKUP_MIN_QUERY) return;
        if (localResultCount >= this._E621_LOOKUP_LOCAL_THRESHOLD) return;
        if (this._e621LookupCache.has(q)) return;  // already cached
        if (this._e621LookupPending && this._e621LookupPending.query === q) return;

        if (this._e621LookupDebounce) {
            clearTimeout(this._e621LookupDebounce);
            this._e621LookupDebounce = null;
        }
        this._e621LookupDebounce = setTimeout(() => {
            this._e621LookupDebounce = null;
            this._fetchE621Lookup(q).catch(() => { /* already logged */ });
        }, this._E621_LOOKUP_DEBOUNCE_MS);
    },

    async _fetchE621Lookup(query) {
        const q = (query || '').trim().toLowerCase();
        if (!q) return;
        if (this._e621LookupCache.has(q)) return;

        const pending = { query: q };
        this._e621LookupPending = pending;
        try {
            const resp = await fetch(`/api/editor/tags/lookup?q=${encodeURIComponent(q)}&limit=10`);
            if (!resp.ok) return;
            const data = await resp.json();
            const matches = Array.isArray(data.matches) ? data.matches : [];
            this._e621LookupCache.set(q, matches);
        } catch (err) {
            // Silent failure — just no suggestions.
            console.debug('e621 lookup failed:', err);
        } finally {
            if (this._e621LookupPending === pending) {
                this._e621LookupPending = null;
            }
            // Re-render dropdown if it's still open with this query.
            const inp = document.getElementById(this._currentTagInputId || 'metadata-tag-input');
            if (this._tagDropdownOpenFor && inp && inp.value.trim().toLowerCase() === q) {
                const results = this._filterTagResults(inp.value);
                this._renderDropdown(results, inp.value);
            }
        }
    },

    /**
     * POST /api/editor/tags/add, then reload the tag DB + immediately add
     * the tag to the active platform so the user's intent (autocomplete
     * this tag) is fulfilled in one click.
     */
    async _addTagToLibrary(name, target) {
        try {
            const resp = await fetch('/api/editor/tags/add', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ name, target, description: '' }),
            });
            if (!resp.ok) {
                let msg = `HTTP ${resp.status}`;
                try {
                    const j = await resp.json();
                    if (j && j.detail) msg = j.detail;
                } catch (_) { /* non-JSON body */ }
                this._setStatus(`Add to library failed: ${msg}`, 'error');
                return false;
            }
            // Force-reload the local tag DB so autocomplete sees the new tag.
            try { sessionStorage.removeItem('pawpoller_tag_db_v1'); } catch (_) {}
            this._tagDb = null;
            try {
                await this._loadTagDb();
            } catch (err) {
                this._setStatus(`Added, but DB reload failed: ${err.message || err}`, 'error');
            }

            // Invalidate e621 cache so the now-in-local-DB tag stops appearing
            // in future suggestion panels.
            this._e621LookupCache.clear();

            // Route through normal add path so cross-platform propagation
            // happens exactly like a native pick.
            this._addTagFromDropdown(name);

            this._setStatus(`Added "${name}" to ${this._targetLabel(target)} library`, 'ok');
            return true;
        } catch (err) {
            this._setStatus(`Add to library failed: ${err.message || err}`, 'error');
            return false;
        }
    },

    _closeDropdown() {
        const dd = document.getElementById('metadata-tag-dropdown-portal');
        if (dd) {
            dd.hidden = true;
            dd.innerHTML = '';
        }
        this._tagDropdownOpenFor = null;
        this._tagDropdownContext = null;
        this._tagDropdownResults = [];
        this._tagDropdownIndex = 0;
        this._currentTagInputId = 'metadata-tag-input';
        // Phase 3b: leave _e621LookupCache (session-scoped) but clear
        // the visible/pending state. Cache still short-circuits re-fetches.
        this._e621LookupResults = [];
        if (this._e621LookupDebounce) {
            clearTimeout(this._e621LookupDebounce);
            this._e621LookupDebounce = null;
        }
    },

    async _openDropdownFor(platform, query, context) {
        this._tagDropdownOpenFor = platform;
        this._tagDropdownIndex = 0;
        // Phase 4b: context defaults to story-level for backwards compat.
        if (context && typeof context === 'object') {
            this._tagDropdownContext = {
                scope: context.scope || 'story',
                platform: context.platform || platform,
                chapterIdx: (typeof context.chapterIdx === 'number') ? context.chapterIdx : null,
            };
            this._currentTagInputId = context.inputId || 'metadata-tag-input';
        } else {
            this._tagDropdownContext = { scope: 'story', platform, chapterIdx: null };
            this._currentTagInputId = 'metadata-tag-input';
        }
        if (!this._tagDb) {
            const dd = this._ensureDropdownPortal();
            dd.innerHTML = `<div class="metadata-tag-result-empty">Loading tag database...</div>`;
            dd.hidden = false;
            this._positionDropdownPortal();
            try {
                await this._loadTagDb();
            } catch (err) {
                dd.innerHTML = `<div class="metadata-tag-result-empty">Failed to load tags: ${this._escape(err.message || err)}</div>`;
                this._positionDropdownPortal();
                return;
            }
            if (this._tagDropdownOpenFor !== platform) return;
        }
        const results = this._filterTagResults(query);
        this._renderDropdown(results, query);
        // Phase 3b: if local matches are sparse, kick off an async e621
        // lookup. When it resolves, _fetchE621Lookup re-renders the dropdown.
        this._maybeTriggerE621Lookup(query, results.length);
    },

    // Platform-specific transforms: convert a default canonical tag into the
    // form expected by each target platform.
    _transformTagForPlatform(canonicalTag, platform) {
        if (platform === 'default') return canonicalTag;
        // Underscore platforms — keep as-is
        if (platform === 'furaffinity' || platform === 'weasyl' || platform === 'itaku') {
            return canonicalTag;
        }
        if (platform === 'wattpad') {
            return canonicalTag.replace(/_([a-z])/g, (_, c) => c.toUpperCase());
        }
        // Space platforms: sofurry, inkbunny, ao3, squidgeworld, deviantart
        return canonicalTag.replace(/_/g, ' ');
    },

    _fixSpacesInTags() {
        let fixed = 0;
        const platforms = ['default', 'furaffinity', 'weasyl', 'itaku'];
        for (const p of platforms) {
            const tags = this.metadata.tags[p];
            if (!Array.isArray(tags)) continue;
            for (let i = 0; i < tags.length; i++) {
                if (tags[i].includes(' ')) {
                    tags[i] = tags[i].replace(/\s+/g, '_');
                    fixed++;
                }
            }
        }
        // Also fix chapter tags
        const chapters = this.metadata.chapter_info || [];
        for (const ch of chapters) {
            if (!ch.tags) continue;
            for (const p of platforms) {
                const tags = ch.tags[p];
                if (!Array.isArray(tags)) continue;
                for (let i = 0; i < tags.length; i++) {
                    if (tags[i].includes(' ')) {
                        tags[i] = tags[i].replace(/\s+/g, '_');
                        fixed++;
                    }
                }
            }
        }
        if (fixed > 0) {
            this._clearStatus();
            this._rerenderTagTabBody();
            this._setStatus(`Fixed ${fixed} tag(s) — spaces replaced with underscores`, 'success');
        } else {
            this._setStatus('No spaces found in tags', 'info');
        }
    },

    _sortTagsAlphabetically() {
        let sorted = 0;
        const allPlats = Object.keys(this.metadata.tags || {});
        for (const p of allPlats) {
            const tags = this.metadata.tags[p];
            if (!Array.isArray(tags) || tags.length < 2) continue;
            const before = tags.join(',');
            tags.sort((a, b) => a.toLowerCase().localeCompare(b.toLowerCase()));
            if (tags.join(',') !== before) sorted++;
        }
        const chapters = this.metadata.chapter_info || [];
        for (const ch of chapters) {
            if (!ch.tags) continue;
            for (const p of Object.keys(ch.tags)) {
                const tags = ch.tags[p];
                if (!Array.isArray(tags) || tags.length < 2) continue;
                const before = tags.join(',');
                tags.sort((a, b) => a.toLowerCase().localeCompare(b.toLowerCase()));
                if (tags.join(',') !== before) sorted++;
            }
        }
        this._clearStatus();
        this._rerenderTagTabBody();
        if (sorted > 0) {
            this._setStatus(`Sorted tags on ${sorted} platform(s) alphabetically`, 'success');
        } else {
            this._setStatus('Tags already sorted', 'info');
        }
    },

    _normalizeTagName(rawName, platform) {
        let name = (rawName || '').trim();
        if (platform === 'default' || platform === 'furaffinity' || platform === 'weasyl' || platform === 'itaku') {
            name = name.replace(/\s+/g, '_');
        }
        return name;
    },

    _populateFromDefault(platform) {
        // One-shot backfill: copy every Default tag into this platform's
        // list, transformed into the platform's expected format (FA/Weasyl
        // get underscores, AO3/SQW/SF/IB get spaces, etc.). Only callable
        // when the platform list is empty — guards against accidentally
        // wiping a deliberate override.
        if (platform === 'default') return;
        const existing = this.metadata.tags[platform] || [];
        if (existing.length > 0) return;
        const defaults = this.metadata.tags.default || [];
        // Defaults in older stories (e.g. Tombstone) sometimes contain spaces
        // rather than the canonical underscored form. Normalise to underscores
        // first, then run the per-platform transform — that way FA/Weasyl get
        // underscores and SF/IB/AO3/SQW get spaces regardless of how the
        // default list was authored.
        const seeded = defaults.map(t => {
            const canonical = t.replace(/\s+/g, '_');
            return this._transformTagForPlatform(canonical, platform);
        });
        // Case-insensitive dedup in case of redundant default entries
        const seen = new Set();
        const deduped = [];
        for (const t of seeded) {
            const k = t.toLowerCase();
            if (seen.has(k)) continue;
            seen.add(k);
            deduped.push(t);
        }
        this.metadata.tags[platform] = deduped;
        this._clearStatus();
        this._rerenderTagTabBody();
    },

    _addTagToPlatform(platform, rawName) {
        const name = this._normalizeTagName(rawName, platform);
        if (!name) return;
        const tags = this.metadata.tags[platform] || [];
        // Case-insensitive dedup
        if (tags.some(t => t.toLowerCase() === name.toLowerCase())) return;

        // If this matches an alias, add the canonical tag instead
        let final = name;
        if (this._tagDb) {
            const alias = this._tagDb.aliases[name.toLowerCase()];
            if (alias && this._tagDb.byName.has(alias)) {
                if (tags.some(t => t.toLowerCase() === alias.toLowerCase())) return;
                final = alias;
            }
        }

        tags.push(final);
        this.metadata.tags[platform] = tags;

        // Default tab is canonical — propagate to EVERY posting platform
        // (not just the UI tabs) with the right per-platform transform.
        if (platform === 'default') {
            for (const p of this.TAG_CASCADE_PLATFORMS) {
                const transformed = this._transformTagForPlatform(final, p);
                const otherTags = this.metadata.tags[p] || [];
                if (!otherTags.some(t => t.toLowerCase() === transformed.toLowerCase())) {
                    otherTags.push(transformed);
                    this.metadata.tags[p] = otherTags;
                }
            }
        }

        this._clearStatus();
        this._rerenderTagTabBody();
        requestAnimationFrame(() => {
            const input = document.getElementById('metadata-tag-input');
            if (input) input.focus();
        });
    },

    _removeTagFromPlatform(platform, index) {
        const tags = this.metadata.tags[platform] || [];
        if (index < 0 || index >= tags.length) return;
        const removed = tags[index];
        tags.splice(index, 1);
        this.metadata.tags[platform] = tags;

        // Default removal cascades to every posting platform (using
        // transformed name), not just the UI-visible tabs.
        if (platform === 'default') {
            for (const p of this.TAG_CASCADE_PLATFORMS) {
                const transformed = this._transformTagForPlatform(removed, p);
                const otherTags = this.metadata.tags[p] || [];
                const idx = otherTags.findIndex(t => t.toLowerCase() === transformed.toLowerCase());
                if (idx >= 0) {
                    otherTags.splice(idx, 1);
                    this.metadata.tags[p] = otherTags;
                }
            }
        }

        this._clearStatus();
        this._rerenderTagTabBody();
    },

    // -----------------------------------------------------------------
    // Phase 4b: chapter-scoped tag writes (NO cross-platform sync —
    // each chapter's per-platform list is independent).
    // -----------------------------------------------------------------

    _addTagToChapter(chapterIdx, platform, rawName) {
        const name = this._normalizeTagName(rawName, platform);
        if (!name) return;
        const entry = this._ensureChapterEntry(chapterIdx);
        if (!entry.tags || typeof entry.tags !== 'object') entry.tags = {};
        const tags = Array.isArray(entry.tags[platform]) ? entry.tags[platform] : [];
        if (tags.some(t => t.toLowerCase() === name.toLowerCase())) return;

        // Alias resolution still applies — write canonical tag if the user
        // typed a known alias.
        let final = name;
        if (this._tagDb) {
            const alias = this._tagDb.aliases[name.toLowerCase()];
            if (alias && this._tagDb.byName.has(alias)) {
                if (tags.some(t => t.toLowerCase() === alias.toLowerCase())) return;
                final = alias;
            }
        }

        tags.push(final);
        entry.tags[platform] = tags;
        this._clearStatus();
        this._rerenderChapterDetail(chapterIdx);
        requestAnimationFrame(() => {
            const inp = document.getElementById(`metadata-tag-input-chapter-${chapterIdx}`);
            if (inp) inp.focus();
        });
    },

    _removeTagFromChapter(chapterIdx, platform, index) {
        const entry = this._getChapterEntry(chapterIdx);
        if (!entry || !entry.tags || !Array.isArray(entry.tags[platform])) return;
        const tags = entry.tags[platform];
        if (index < 0 || index >= tags.length) return;
        tags.splice(index, 1);
        entry.tags[platform] = tags;
        this._clearStatus();
        this._rerenderChapterDetail(chapterIdx);
    },

    /**
     * Silent mutation wrappers used by the tag browser modal — mirror the
     * add/remove chapter helpers but don't rerender the chapter detail
     * (caller handles rerenders so focus isn't stolen).
     */
    _addTagToChapterSilent(chapterIdx, platform, rawName) {
        const name = (rawName || '').trim();
        if (!name) return;
        const entry = this._ensureChapterEntry(chapterIdx);
        if (!entry.tags || typeof entry.tags !== 'object') entry.tags = {};
        const tags = Array.isArray(entry.tags[platform]) ? entry.tags[platform] : [];
        if (tags.some(t => t.toLowerCase() === name.toLowerCase())) return;
        let final = name;
        if (this._tagDb) {
            const alias = this._tagDb.aliases[name.toLowerCase()];
            if (alias && this._tagDb.byName.has(alias)) {
                if (tags.some(t => t.toLowerCase() === alias.toLowerCase())) return;
                final = alias;
            }
        }
        tags.push(final);
        entry.tags[platform] = tags;
        this._clearStatus();
    },

    _removeTagFromChapterSilent(chapterIdx, platform, index) {
        const entry = this._getChapterEntry(chapterIdx);
        if (!entry || !entry.tags || !Array.isArray(entry.tags[platform])) return;
        const tags = entry.tags[platform];
        if (index < 0 || index >= tags.length) return;
        tags.splice(index, 1);
        entry.tags[platform] = tags;
        this._clearStatus();
    },

    /**
     * Context-aware add — routes to _addTagToPlatform (story scope) or
     * _addTagToChapter (chapter scope). Called from the dropdown portal
     * click / Enter handlers.
     */
    _addTagFromDropdown(rawName) {
        const ctx = this._tagDropdownContext;
        if (!ctx) return;
        if (ctx.scope === 'chapter' && typeof ctx.chapterIdx === 'number') {
            this._addTagToChapter(ctx.chapterIdx, ctx.platform, rawName);
        } else {
            this._addTagToPlatform(ctx.platform, rawName);
        }
    },

    _bindTagTabBodyEvents() {
        // Remove-pill buttons
        document.querySelectorAll('[data-tag-remove]').forEach(btn => {
            btn.addEventListener('click', (e) => {
                e.preventDefault();
                const platform = btn.getAttribute('data-tag-remove');
                const idx = parseInt(btn.getAttribute('data-index'), 10);
                if (!Number.isNaN(idx)) this._removeTagFromPlatform(platform, idx);
            });
        });

        // Populate-from-default button (only present on empty non-default tabs
        // for stories whose story.json predates these tabs)
        document.getElementById('metadata-tag-populate-btn')?.addEventListener('click', (e) => {
            e.preventDefault();
            const platform = e.currentTarget.getAttribute('data-populate-platform');
            this._populateFromDefault(platform);
        });

        const input = document.getElementById('metadata-tag-input');
        if (!input) return;
        const platform = input.getAttribute('data-tag-platform-input');
        const storyContext = { scope: 'story', platform, chapterIdx: null, inputId: 'metadata-tag-input' };

        input.addEventListener('focus', () => {
            this._openDropdownFor(platform, input.value, storyContext);
        });

        input.addEventListener('input', () => {
            if (platform === 'default' || platform === 'furaffinity' || platform === 'weasyl' || platform === 'itaku') {
                const pos = input.selectionStart;
                const fixed = input.value.replace(/ /g, '_');
                if (fixed !== input.value) {
                    input.value = fixed;
                    input.selectionStart = input.selectionEnd = pos;
                }
            }
            this._openDropdownFor(platform, input.value, storyContext);
        });

        input.addEventListener('keydown', (e) => {
            const results = this._tagDropdownResults;
            if (e.key === 'ArrowDown') {
                e.preventDefault();
                if (!results.length) return;
                this._tagDropdownIndex = Math.min(results.length - 1, this._tagDropdownIndex + 1);
                this._renderDropdown(results, input.value);
            } else if (e.key === 'ArrowUp') {
                e.preventDefault();
                if (!results.length) return;
                this._tagDropdownIndex = Math.max(0, this._tagDropdownIndex - 1);
                this._renderDropdown(results, input.value);
            } else if (e.key === 'Enter') {
                e.preventDefault();
                const picked = results[this._tagDropdownIndex];
                if (picked) {
                    this._addTagToPlatform(platform, picked.tag.name);
                } else if (input.value.trim()) {
                    // No matches — add raw (allows arbitrary tags)
                    this._addTagToPlatform(platform, input.value);
                }
                // _rerenderTagTabBody replaces the input; nothing else to clear
            } else if (e.key === 'Escape') {
                this._closeDropdown();
                input.blur();
            } else if (e.key === 'Backspace' && input.value === '') {
                // Remove last pill
                const tags = this.metadata.tags[platform] || [];
                if (tags.length) {
                    tags.pop();
                    this.metadata.tags[platform] = tags;
                    this._clearStatus();
                    this._rerenderTagTabBody();
                }
            }
        });

        input.addEventListener('blur', () => {
            // Small delay so click-on-result registers before we close
            setTimeout(() => this._closeDropdown(), 150);
        });

        this._ensureDropdownPortalHandlers();
    },

    /**
     * Bind the portal-level mousedown/click handlers exactly once. Routes
     * clicks on result rows + "Browse all" based on the current context,
     * so it works for story tabs AND per-chapter inputs.
     */
    _ensureDropdownPortalHandlers() {
        const dd = this._ensureDropdownPortal();
        if (!dd || dd._handlersBound) return;
        dd.addEventListener('mousedown', (e) => {
            // Prevent blur from firing before click
            e.preventDefault();
        });
        dd.addEventListener('click', (e) => {
            const ctx = this._tagDropdownContext;
            const actionBtn = e.target.closest('[data-action="open-tag-browser"]');
            if (actionBtn) {
                e.preventDefault();
                const inp = document.getElementById(this._currentTagInputId || 'metadata-tag-input');
                const q = inp ? inp.value : '';
                const browserCtx = ctx ? { ...ctx } : null;
                this._closeDropdown();
                this._openTagBrowser(q, browserCtx);
                return;
            }

            // Phase 3b: e621 "+ Library" / "Use once" buttons.
            const addBtn = e.target.closest('[data-action="add-to-library"]');
            if (addBtn) {
                e.preventDefault();
                const name = addBtn.getAttribute('data-e621-name');
                const target = addBtn.getAttribute('data-target') || 'user';
                if (name) this._addTagToLibrary(name, target);
                return;
            }
            const useOnceBtn = e.target.closest('[data-action="use-once"]');
            if (useOnceBtn) {
                e.preventDefault();
                const name = useOnceBtn.getAttribute('data-e621-name');
                if (name) this._addTagFromDropdown(name);
                return;
            }
            const caretBtn = e.target.closest('[data-action="toggle-target-menu"]');
            if (caretBtn) {
                e.preventDefault();
                const idx = caretBtn.getAttribute('data-e621-index');
                const menu = dd.querySelector(`[data-target-menu-for="${idx}"]`);
                if (menu) {
                    // Close any other open menus first
                    dd.querySelectorAll('[data-target-menu-for]').forEach(m => {
                        if (m !== menu) m.hidden = true;
                    });
                    menu.hidden = !menu.hidden;
                }
                return;
            }

            const row = e.target.closest('[data-tag-result-index]');
            if (!row) return;
            const idx = parseInt(row.getAttribute('data-tag-result-index'), 10);
            const picked = this._tagDropdownResults[idx];
            if (!picked) return;
            this._addTagFromDropdown(picked.tag.name);
        });
        dd._handlersBound = true;
    },

    _truncate(s, n) {
        if (!s) return '';
        return s.length > n ? (s.slice(0, n - 1) + '\u2026') : s;
    },

    // ---------------------------------------------------------------------
    // Section 4b: Expanded Tag Browser Modal (Phase 3a+)
    //
    // Full-screen modal giving users a card grid view of all matching tags
    // with category filter chips + pagination. Mounted on document.body
    // (not inside the drawer) so it can overlay at a higher z-index.
    // ---------------------------------------------------------------------

    /**
     * Public entry point — opens the expanded tag browser for the currently
     * active tag platform. Can be called from anywhere once the drawer is
     * open (e.g. via console, or by future keyboard shortcuts).
     */
    openTagBrowser(query) {
        this._openTagBrowser(query || '');
    },

    async _openTagBrowser(initialQuery, context) {
        if (!this.isOpen) return;
        this._tagBrowserOpen = true;
        this._tagBrowserQuery = initialQuery || '';
        this._tagBrowserFilters = new Set();
        this._tagBrowserPage = 1;
        // Phase 4b: remember where browser writes to. null = story-level
        // using _activeTagPlatform (legacy behaviour).
        this._tagBrowserContext = (context && typeof context === 'object') ? {
            scope: context.scope || 'story',
            platform: context.platform || this._activeTagPlatform,
            chapterIdx: (typeof context.chapterIdx === 'number') ? context.chapterIdx : null,
        } : null;

        // Mount shell immediately with a loading state so the modal pops
        // up even if the tag DB is still loading.
        this._mountTagBrowser();

        if (!this._tagDb) {
            try {
                await this._loadTagDb();
            } catch (err) {
                const host = document.getElementById('tag-browser-results');
                if (host) host.innerHTML = `<div class="tag-browser-empty">Failed to load tags: ${this._escape(err.message || err)}</div>`;
                return;
            }
            // If closed during load, bail
            if (!this._tagBrowserOpen) return;
        }
        this._renderTagBrowserResults();
        this._updateTagBrowserFilterCounts();
        this._updateTagBrowserSelectedStrip();
        this._updateTagBrowserFooter();
    },

    _closeTagBrowser() {
        if (!this._tagBrowserOpen) return;
        this._tagBrowserOpen = false;
        const root = document.getElementById('tag-browser-root');
        if (root) {
            const modal = root.querySelector('.tag-browser-modal');
            if (modal) modal.classList.remove('open');
            setTimeout(() => root.remove(), 200);
        }
        // Unhook escape handler
        if (this._tagBrowserKeyHandler) {
            document.removeEventListener('keydown', this._tagBrowserKeyHandler);
            this._tagBrowserKeyHandler = null;
        }
        // Re-render the underlying drawer tag pills so they reflect any
        // additions/removals made through the browser. For chapter scope,
        // rerender just the chapter's tag body.
        const bctx = this._tagBrowserContext;
        if (bctx && bctx.scope === 'chapter' && typeof bctx.chapterIdx === 'number') {
            this._rerenderChapterTagBody(bctx.chapterIdx);
        } else {
            this._rerenderTagTabBody();
        }
        this._tagBrowserContext = null;
    },

    /**
     * Phase 4b: return the platform the browser is currently writing to
     * + the tag array it's reading from, factoring in chapter scope.
     */
    _tagBrowserTargetPlatform() {
        const ctx = this._tagBrowserContext;
        if (ctx && ctx.scope === 'chapter') return ctx.platform;
        return this._activeTagPlatform;
    },

    _tagBrowserTargetTags() {
        const ctx = this._tagBrowserContext;
        if (ctx && ctx.scope === 'chapter' && typeof ctx.chapterIdx === 'number') {
            const entry = this._getChapterEntry(ctx.chapterIdx);
            if (entry && entry.tags && Array.isArray(entry.tags[ctx.platform])) {
                return entry.tags[ctx.platform];
            }
            return [];
        }
        return this.metadata.tags[this._activeTagPlatform] || [];
    },

    _mountTagBrowser() {
        // Remove any stale instance
        const stale = document.getElementById('tag-browser-root');
        if (stale) stale.remove();

        const root = document.createElement('div');
        root.id = 'tag-browser-root';
        root.innerHTML = this._renderTagBrowser();
        document.body.appendChild(root);

        // Slide-in on next frame so CSS transition plays
        requestAnimationFrame(() => {
            root.querySelector('.tag-browser-modal')?.classList.add('open');
            const search = document.getElementById('tag-browser-search');
            if (search) {
                search.focus();
                // Put cursor at end so typing appends
                const v = search.value;
                search.value = '';
                search.value = v;
            }
        });

        this._bindTagBrowserEvents();
    },

    _renderTagBrowser() {
        const platform = this._tagBrowserTargetPlatform();
        const platformLabel = this.PLATFORM_LABELS[platform] || platform;
        const q = this._tagBrowserQuery || '';
        const filters = this._tagBrowserFilters;

        // Chapter-scoped title includes the chapter number + title
        const bctx = this._tagBrowserContext;
        let titlePrefix = 'Browse Tags';
        if (bctx && bctx.scope === 'chapter' && typeof bctx.chapterIdx === 'number') {
            const ch = this._chapterData && this._chapterData.chapters.find(c => c.index === bctx.chapterIdx);
            const chEntry = this._getChapterEntry(bctx.chapterIdx);
            const chTitle = (chEntry && chEntry.title) || (ch && (ch.title_from_md || ch.title)) || `Chapter ${bctx.chapterIdx}`;
            titlePrefix = `Browse Tags &mdash; Chapter ${bctx.chapterIdx}: ${this._escape(chTitle)}`;
        }

        const chipKeys = ['all', 'selected', ...this._TAG_BROWSER_CATEGORIES];
        const chips = chipKeys.map(cat => {
            const active = (cat === 'all' && filters.size === 0) || filters.has(cat);
            const label = cat === 'all' ? 'All' : cat === 'selected' ? 'Selected' : (cat.charAt(0).toUpperCase() + cat.slice(1));
            return `<button type="button" class="tag-browser-chip${active ? ' tag-browser-chip-active' : ''}" data-tb-filter="${this._escape(cat)}"><span class="tag-browser-chip-label">${this._escape(label)}</span> <span class="tag-browser-chip-count" data-tb-count="${this._escape(cat)}"></span></button>`;
        }).join('');

        return `
            <div class="tag-browser-backdrop" data-tb-backdrop></div>
            <div class="tag-browser-modal" role="dialog" aria-label="Browse tags">
                <div class="tag-browser-header">
                    <div class="tag-browser-title-row">
                        <div class="tag-browser-title">${titlePrefix} &mdash; ${this._escape(platformLabel)}</div>
                        <button type="button" class="tag-browser-close" data-tb-close aria-label="Close">&times;</button>
                    </div>
                    <input type="text" id="tag-browser-search" class="tag-browser-search" placeholder="Search tags..." value="${this._escape(q)}" autocomplete="off" />
                    <div class="tag-browser-filters">${chips}</div>
                </div>
                <div class="tag-browser-selected" id="tag-browser-selected"></div>
                <div class="tag-browser-body">
                    <div class="tag-browser-grid" id="tag-browser-results">
                        <div class="tag-browser-empty">Loading...</div>
                    </div>
                    <div class="tag-browser-loadmore-wrap" id="tag-browser-loadmore-wrap"></div>
                </div>
                <div class="tag-browser-footer">
                    <div class="tag-browser-count" id="tag-browser-count"></div>
                    <button type="button" class="btn btn-sm" data-tb-close>Done</button>
                </div>
            </div>
        `;
    },

    _bindTagBrowserEvents() {
        const root = document.getElementById('tag-browser-root');
        if (!root) return;

        // Close (×, Done, backdrop)
        root.querySelectorAll('[data-tb-close]').forEach(el => {
            el.addEventListener('click', (e) => {
                e.preventDefault();
                this._closeTagBrowser();
            });
        });
        const backdrop = root.querySelector('[data-tb-backdrop]');
        if (backdrop) {
            backdrop.addEventListener('click', () => this._closeTagBrowser());
        }

        // Escape key
        this._tagBrowserKeyHandler = (e) => {
            if (e.key === 'Escape' && this._tagBrowserOpen) {
                e.preventDefault();
                this._closeTagBrowser();
            }
        };
        document.addEventListener('keydown', this._tagBrowserKeyHandler);

        // Search input — live filter
        const search = document.getElementById('tag-browser-search');
        if (search) {
            search.addEventListener('input', () => {
                this._tagBrowserQuery = search.value;
                this._tagBrowserPage = 1;
                this._renderTagBrowserResults();
                this._updateTagBrowserFilterCounts();
            });
        }

        // Filter chips — toggle multi-select
        root.querySelectorAll('[data-tb-filter]').forEach(btn => {
            btn.addEventListener('click', () => {
                const cat = btn.getAttribute('data-tb-filter');
                if (cat === 'all') {
                    this._tagBrowserFilters.clear();
                } else if (cat === 'selected') {
                    if (this._tagBrowserFilters.has('selected')) {
                        this._tagBrowserFilters.clear();
                    } else {
                        this._tagBrowserFilters.clear();
                        this._tagBrowserFilters.add('selected');
                    }
                } else {
                    this._tagBrowserFilters.delete('selected');
                    if (this._tagBrowserFilters.has(cat)) {
                        this._tagBrowserFilters.delete(cat);
                    } else {
                        this._tagBrowserFilters.add(cat);
                    }
                }
                this._tagBrowserPage = 1;
                this._updateTagBrowserChipStates();
                this._renderTagBrowserResults();
            });
        });
    },

    _updateTagBrowserChipStates() {
        const root = document.getElementById('tag-browser-root');
        if (!root) return;
        const filters = this._tagBrowserFilters;
        root.querySelectorAll('[data-tb-filter]').forEach(btn => {
            const cat = btn.getAttribute('data-tb-filter');
            const active = (cat === 'all' && filters.size === 0) || filters.has(cat);
            btn.classList.toggle('tag-browser-chip-active', active);
        });
    },

    /**
     * Build the filtered list of tags based on current query + category
     * filters. Returns the full list (unpaginated) — pagination applied
     * by the renderer.
     */
    _filterTagBrowserResults() {
        if (!this._tagDb) return [];
        const q = (this._tagBrowserQuery || '').toLowerCase().trim();
        const filters = this._tagBrowserFilters;
        const hasCat = filters.size > 0;
        const isSelectedFilter = filters.has('selected');
        const { names } = this._tagDb;

        const selectedSet = isSelectedFilter
            ? new Set(this._tagBrowserTargetTags().map(t => t.toLowerCase()))
            : null;

        // Ranking: exact match → prefix → substring → alphabetical (within buckets)
        const exact = [];
        const prefix = [];
        const substring = [];
        const all = [];

        for (const entry of names) {
            if (isSelectedFilter) {
                if (!selectedSet.has(entry.lower)) continue;
            } else if (hasCat && !filters.has(entry.tag.category)) continue;
            if (!q) {
                all.push(entry.tag);
            } else if (entry.lower === q) {
                exact.push(entry.tag);
            } else if (entry.lower.startsWith(q)) {
                prefix.push(entry.tag);
            } else if (entry.lower.includes(q)) {
                substring.push(entry.tag);
            }
        }

        // Category priority: fiction first (physical/acts/kink/meta), image last
        const catPriority = { physical: 0, acts: 1, kink: 2, meta: 3, image: 4 };
        const sortByCatThenName = (a, b) => {
            const pa = catPriority[a.category] ?? 99;
            const pb = catPriority[b.category] ?? 99;
            if (pa !== pb) return pa - pb;
            return a.name.localeCompare(b.name);
        };

        let results;
        if (!q) {
            all.sort(sortByCatThenName);
            results = all;
        } else {
            exact.sort(sortByCatThenName);
            prefix.sort(sortByCatThenName);
            substring.sort(sortByCatThenName);
            results = [...exact, ...prefix, ...substring];
        }

        if (isSelectedFilter) {
            const dbLower = new Set(results.map(t => t.name.toLowerCase()));
            for (const t of this._tagBrowserTargetTags()) {
                if (!dbLower.has(t.toLowerCase())) {
                    if (!q || t.toLowerCase().includes(q)) {
                        results.push({ name: t, desc: '', category: 'user' });
                    }
                }
            }
        }
        return results;
    },

    _renderE621CardsBlock() {
        const q = (this._tagBrowserQuery || '').trim().toLowerCase();
        if (!q || q.length < this._E621_LOOKUP_MIN_QUERY) return '';

        // Trigger lookup if not cached
        if (!this._e621LookupCache.has(q) && (!this._e621LookupPending || this._e621LookupPending.query !== q)) {
            this._fetchE621Lookup(q).then(() => {
                if (this._tagBrowserOpen && (this._tagBrowserQuery || '').trim().toLowerCase() === q) {
                    this._renderTagBrowserResults();
                }
            }).catch(() => {});
            return `<div class="tag-browser-e621-loading">&#128218; Searching e621 for more suggestions&hellip;</div>`;
        }

        const matches = this._e621LookupCache.get(q);
        if (!matches || !matches.length) return '';

        const platform = this._tagBrowserTargetPlatform();
        const platformTags = this._tagBrowserTargetTags().map(t => t.toLowerCase());
        const platformTagSet = new Set(platformTags);

        const cards = matches.map(m => {
            const transformed = this._transformTagForPlatform(m.name, platform).toLowerCase();
            const isAdded = platformTagSet.has(m.name.toLowerCase()) || platformTagSet.has(transformed);
            const addedCls = isAdded ? ' tag-browser-card-added' : '';
            const catLabel = this._e621CategoryLabel(m.category);
            const suggestedTarget = this._suggestedTargetForE621Cat(m.category);
            const count = (m.post_count || 0).toLocaleString();
            return `
                <div class="tag-browser-card tag-browser-card-e621${addedCls}" data-e621-name="${this._escape(m.name)}">
                    <div class="tag-browser-card-head">
                        <div class="tag-browser-card-name">${this._escape(m.name)}</div>
                        <span class="tag-browser-card-cat metadata-tag-cat-e621">e621 ${this._escape(catLabel)}</span>
                    </div>
                    <div class="tag-browser-card-meta">${count} posts</div>
                    <div class="tag-browser-card-footer">
                        <button type="button" class="tag-browser-card-btn" data-action="add-to-library" data-e621-name="${this._escape(m.name)}" data-target="${this._escape(suggestedTarget)}">+ Library (${this._escape(this._targetLabel(suggestedTarget))})</button>
                        <button type="button" class="tag-browser-card-btn tag-browser-card-btn-secondary" data-action="use-once" data-e621-name="${this._escape(m.name)}">Use once</button>
                    </div>
                </div>
            `;
        }).join('');
        return `<div class="tag-browser-e621-divider">&#128218; e621 suggestions (not in your library)</div><div class="tag-browser-grid">${cards}</div>`;
    },

    _bindE621CardActions(host) {
        if (!host) return;
        host.querySelectorAll('[data-e621-name]').forEach(card => {
            // Buttons inside cards
            card.querySelectorAll('[data-action]').forEach(btn => {
                btn.addEventListener('click', async (e) => {
                    e.preventDefault();
                    e.stopPropagation();
                    const action = btn.getAttribute('data-action');
                    const name = btn.getAttribute('data-e621-name');
                    if (action === 'add-to-library') {
                        const target = btn.getAttribute('data-target');
                        const ok = await this._addTagToLibrary(name, target);
                        if (ok) this._renderTagBrowserResults();
                    } else if (action === 'use-once') {
                        // Add to platform without library mutation
                        const platform = this._tagBrowserTargetPlatform();
                        const ctx = this._tagBrowserContext;
                        if (ctx && ctx.scope === 'chapter') {
                            this._addTagToChapterSilent(ctx.chapterIdx, platform, name);
                        } else {
                            this._addTagToPlatformSilent(platform, name);
                        }
                        this._renderTagBrowserResults();
                        this._updateTagBrowserSelectedStrip();
                        this._updateTagBrowserFooter();
                    }
                });
            });
        });
    },

    _renderTagBrowserResults() {
        const host = document.getElementById('tag-browser-results');
        const loadMoreWrap = document.getElementById('tag-browser-loadmore-wrap');
        if (!host) return;

        const all = this._filterTagBrowserResults();
        const pageSize = this._TAG_BROWSER_PAGE_SIZE;
        const limit = pageSize * this._tagBrowserPage;
        const shown = all.slice(0, limit);

        if (!shown.length) {
            const q = (this._tagBrowserQuery || '').trim();
            const e621Block = this._renderE621CardsBlock();
            host.innerHTML = `<div class="tag-browser-empty">No local tags match${q ? ` "${this._escape(q)}"` : ''}.</div>${e621Block}`;
            if (loadMoreWrap) loadMoreWrap.innerHTML = '';
            this._bindE621CardActions(host);
            this._updateTagBrowserFooter(all.length);
            return;
        }

        const platform = this._tagBrowserTargetPlatform();
        const platformTags = this._tagBrowserTargetTags().map(t => t.toLowerCase());
        const platformTagSet = new Set(platformTags);

        const allPlatKeys = ['default', ...this.TAG_CASCADE_PLATFORMS];
        const platLabels = { default: 'DEF', sofurry: 'SF', inkbunny: 'IB', wattpad: 'WP',
            ao3: 'AO3', squidgeworld: 'SQW', weasyl: 'WS', furaffinity: 'FA',
            deviantart: 'DA', itaku: 'IK' };
        const platTagSets = {};
        for (const p of allPlatKeys) {
            const tags = (this.metadata.tags[p] || []).map(t => t.toLowerCase());
            if (tags.length) platTagSets[p] = new Set(tags);
        }

        const cards = shown.map(t => {
            const canonical = t.name;
            const transformed = this._transformTagForPlatform(canonical, platform).toLowerCase();
            const isAdded = platformTagSet.has(canonical.toLowerCase()) || platformTagSet.has(transformed);
            const addedCls = isAdded ? ' tag-browser-card-added' : '';
            const btnLabel = isAdded ? '&#10003; Added' : '+ Add';
            const btnCls = isAdded ? 'tag-browser-card-btn tag-browser-card-btn-added' : 'tag-browser-card-btn';
            const desc = t.desc ? `<div class="tag-browser-card-desc">${this._escape(t.desc)}</div>` : '';

            const badges = allPlatKeys.map(p => {
                const pTransformed = this._transformTagForPlatform(canonical, p).toLowerCase();
                const pSet = platTagSets[p];
                if (!pSet) return '';
                const on = pSet.has(canonical.toLowerCase()) || pSet.has(pTransformed);
                if (!on) return '';
                const cls = p === platform ? 'tag-browser-plat-badge tag-browser-plat-badge-active' : 'tag-browser-plat-badge';
                return `<span class="${cls}">${platLabels[p] || p}</span>`;
            }).filter(Boolean).join('');
            const badgeRow = badges ? `<div class="tag-browser-card-platforms">${badges}</div>` : '';

            return `
                <div class="tag-browser-card${addedCls}" data-tb-tag="${this._escape(canonical)}">
                    <div class="tag-browser-card-head">
                        <div class="tag-browser-card-name">${this._escape(canonical)}</div>
                        <span class="tag-browser-card-cat metadata-tag-cat-${this._escape(t.category)}">${this._escape(t.category)}</span>
                    </div>
                    ${desc}${badgeRow}
                    <div class="tag-browser-card-footer">
                        <button type="button" class="${btnCls}" data-tb-toggle="${this._escape(canonical)}">${btnLabel}</button>
                    </div>
                </div>
            `;
        }).join('');

        // Append e621 suggestions only when we've reached the last page
        // (so they appear at the very bottom, after everything else loaded)
        const e621Block = (shown.length === all.length) ? this._renderE621CardsBlock() : '';
        host.innerHTML = cards + e621Block;
        this._bindE621CardActions(host);

        // "Load more" button
        if (loadMoreWrap) {
            if (all.length > shown.length) {
                loadMoreWrap.innerHTML = `
                    <button type="button" class="btn btn-sm btn-outline tag-browser-loadmore" data-tb-loadmore>
                        Load ${Math.min(pageSize, all.length - shown.length)} more (${shown.length} of ${all.length})
                    </button>
                `;
                const btn = loadMoreWrap.querySelector('[data-tb-loadmore]');
                if (btn) {
                    btn.addEventListener('click', () => {
                        this._tagBrowserPage += 1;
                        this._renderTagBrowserResults();
                    });
                }
            } else {
                loadMoreWrap.innerHTML = '';
            }
        }

        // Wire card interactions — click card or button toggles add/remove
        host.querySelectorAll('[data-tb-tag]').forEach(card => {
            const tagName = card.getAttribute('data-tb-tag');
            card.addEventListener('click', (e) => {
                // Ignore clicks on the button directly — button has its own
                // handler to avoid a double-fire. Matching [data-tb-toggle]
                // ancestor catches clicks on text inside the button too.
                if (e.target.closest('[data-tb-toggle]')) return;
                this._tagBrowserToggleTag(tagName);
            });
        });
        host.querySelectorAll('[data-tb-toggle]').forEach(btn => {
            btn.addEventListener('click', (e) => {
                e.preventDefault();
                e.stopPropagation();
                const tagName = btn.getAttribute('data-tb-toggle');
                this._tagBrowserToggleTag(tagName);
            });
        });

        this._updateTagBrowserFooter(all.length);
    },

    _tagBrowserToggleTag(canonicalName) {
        const bctx = this._tagBrowserContext;
        const isChapter = bctx && bctx.scope === 'chapter' && typeof bctx.chapterIdx === 'number';
        const platform = this._tagBrowserTargetPlatform();
        const tags = this._tagBrowserTargetTags();

        // For chapter scope, there's no default→others cascade, so we
        // only check canonical name. For story scope, also check transformed
        // (matches existing behaviour).
        const lcCanonical = canonicalName.toLowerCase();
        let idx;
        if (isChapter) {
            idx = tags.findIndex(t => t.toLowerCase() === lcCanonical);
        } else {
            const transformed = this._transformTagForPlatform(canonicalName, platform);
            const lcTransformed = transformed.toLowerCase();
            idx = tags.findIndex(t => {
                const lc = t.toLowerCase();
                return lc === lcCanonical || lc === lcTransformed;
            });
        }

        if (idx >= 0) {
            if (isChapter) {
                this._removeTagFromChapterSilent(bctx.chapterIdx, platform, idx);
            } else {
                this._removeTagFromPlatformSilent(platform, idx);
            }
        } else {
            if (isChapter) {
                this._addTagToChapterSilent(bctx.chapterIdx, platform, canonicalName);
            } else {
                this._addTagToPlatformSilent(platform, canonicalName);
            }
        }

        this._renderTagBrowserResults();
        this._updateTagBrowserSelectedStrip();
        this._updateTagBrowserFooter();
    },

    /**
     * Mutation wrappers that mirror _addTagToPlatform / _removeTagFromPlatform
     * but DON'T rerender the drawer tag body (that happens lazily on close,
     * to avoid stealing focus from the browser modal).
     */
    _addTagToPlatformSilent(platform, rawName) {
        const name = (rawName || '').trim();
        if (!name) return;
        const tags = this.metadata.tags[platform] || [];
        if (tags.some(t => t.toLowerCase() === name.toLowerCase())) return;

        let final = name;
        if (this._tagDb) {
            const alias = this._tagDb.aliases[name.toLowerCase()];
            if (alias && this._tagDb.byName.has(alias)) {
                if (tags.some(t => t.toLowerCase() === alias.toLowerCase())) return;
                final = alias;
            }
        }

        tags.push(final);
        this.metadata.tags[platform] = tags;

        if (platform === 'default') {
            for (const p of this.TAG_PLATFORMS) {
                if (p === 'default') continue;
                const transformed = this._transformTagForPlatform(final, p);
                const otherTags = this.metadata.tags[p] || [];
                if (!otherTags.some(t => t.toLowerCase() === transformed.toLowerCase())) {
                    otherTags.push(transformed);
                    this.metadata.tags[p] = otherTags;
                }
            }
        }
        this._clearStatus();
    },

    _removeTagFromPlatformSilent(platform, index) {
        const tags = this.metadata.tags[platform] || [];
        if (index < 0 || index >= tags.length) return;
        const removed = tags[index];
        tags.splice(index, 1);
        this.metadata.tags[platform] = tags;

        if (platform === 'default') {
            for (const p of this.TAG_PLATFORMS) {
                if (p === 'default') continue;
                const transformed = this._transformTagForPlatform(removed, p);
                const otherTags = this.metadata.tags[p] || [];
                const idx = otherTags.findIndex(t => t.toLowerCase() === transformed.toLowerCase());
                if (idx >= 0) {
                    otherTags.splice(idx, 1);
                    this.metadata.tags[p] = otherTags;
                }
            }
        }
        this._clearStatus();
    },

    _updateTagBrowserFilterCounts() {
        const root = document.getElementById('tag-browser-root');
        if (!root || !this._tagDb) return;
        const q = (this._tagBrowserQuery || '').toLowerCase().trim();
        const counts = { all: 0, selected: 0, physical: 0, acts: 0, kink: 0, meta: 0, image: 0 };
        const selectedSet = new Set(this._tagBrowserTargetTags().map(t => t.toLowerCase()));
        for (const entry of this._tagDb.names) {
            if (q && !entry.lower.includes(q)) continue;
            counts.all += 1;
            if (counts[entry.tag.category] !== undefined) counts[entry.tag.category] += 1;
            if (selectedSet.has(entry.lower)) counts.selected += 1;
        }
        // Also count selected tags not in the DB (user-added/arbitrary)
        const dbNames = new Set(this._tagDb.names.map(e => e.lower));
        for (const t of this._tagBrowserTargetTags()) {
            if (!dbNames.has(t.toLowerCase()) && (!q || t.toLowerCase().includes(q))) {
                counts.selected += 1;
            }
        }
        root.querySelectorAll('[data-tb-count]').forEach(el => {
            const cat = el.getAttribute('data-tb-count');
            const n = counts[cat] || 0;
            el.textContent = `(${n.toLocaleString()})`;
        });
    },

    _updateTagBrowserSelectedStrip() {
        const host = document.getElementById('tag-browser-selected');
        if (!host) return;
        const bctx = this._tagBrowserContext;
        const isChapter = bctx && bctx.scope === 'chapter' && typeof bctx.chapterIdx === 'number';
        const platform = this._tagBrowserTargetPlatform();
        const tags = this._tagBrowserTargetTags();
        if (!tags.length) {
            host.innerHTML = `<div class="tag-browser-selected-empty">No tags on ${this._escape(this.PLATFORM_LABELS[platform] || platform)} yet.</div>`;
            return;
        }
        const pills = tags.map((t, i) => `
            <span class="tag-browser-selected-pill" data-tb-selected-index="${i}">
                <span>${this._escape(t)}</span>
                <button type="button" class="tag-browser-selected-remove" data-tb-remove-selected="${i}" aria-label="Remove">&times;</button>
            </span>
        `).join('');
        host.innerHTML = `
            <div class="tag-browser-selected-label">On ${this._escape(this.PLATFORM_LABELS[platform] || platform)}:</div>
            <div class="tag-browser-selected-pills">${pills}</div>
        `;
        host.querySelectorAll('[data-tb-remove-selected]').forEach(btn => {
            btn.addEventListener('click', (e) => {
                e.preventDefault();
                const idx = parseInt(btn.getAttribute('data-tb-remove-selected'), 10);
                if (Number.isNaN(idx)) return;
                if (isChapter) {
                    this._removeTagFromChapterSilent(bctx.chapterIdx, platform, idx);
                } else {
                    this._removeTagFromPlatformSilent(platform, idx);
                }
                this._renderTagBrowserResults();
                this._updateTagBrowserSelectedStrip();
                this._updateTagBrowserFooter();
            });
        });
    },

    _updateTagBrowserFooter(totalMatches) {
        const el = document.getElementById('tag-browser-count');
        if (!el) return;
        const platform = this._tagBrowserTargetPlatform();
        const count = this._tagBrowserTargetTags().length;
        const limit = this.TAG_LIMITS[platform];
        const limitLabel = (limit === Infinity) ? '\u221E' : limit;
        const overLimit = (limit !== Infinity) && count > limit;
        const matchFrag = (typeof totalMatches === 'number') ? ` &middot; ${totalMatches.toLocaleString()} matches` : '';
        el.innerHTML = `<span class="${overLimit ? 'tag-browser-count-over' : ''}">Selected: ${count} &middot; Platform max: ${this._escape(String(limitLabel))}</span>${matchFrag}`;
    },

    // ---------------------------------------------------------------------
    // Section 5: Platform Toggles
    // ---------------------------------------------------------------------

    _renderPlatformTogglesSection() {
        const md = this.metadata;

        const rows = this.PLATFORMS.map(p => {
            const checked = md.platforms[p] ? ' checked' : '';
            const id = `meta-platform-${p}`;
            return `
                <label class="metadata-checkbox" for="${id}">
                    <input type="checkbox" id="${id}" data-platform-toggle="${this._escape(p)}"${checked} />
                    <span>${this._escape(this.PLATFORM_LABELS[p] || p)}</span>
                </label>
            `;
        }).join('');

        return `
            <section class="metadata-section" data-section="platforms" data-expanded="false">
                <button type="button" class="metadata-section-header" data-section-toggle="platforms">
                    <span class="metadata-section-chevron">&#9654;</span>
                    <span>Platform Toggles</span>
                </button>
                <div class="metadata-section-body">
                    <div class="metadata-checkbox-list">
                        ${rows}
                    </div>
                </div>
            </section>
        `;
    },

    // ---------------------------------------------------------------------
    // Pill input component (characters, relationships, etc.)
    // ---------------------------------------------------------------------

    /**
     * Render a tag-pill input. Pills come first, then an input field that
     * grows to fill the rest of the row.
     *
     * @param {string} fieldName  Metadata key (e.g. 'characters') that holds
     *                            the backing array on this.metadata.
     * @param {string[]} values   Current pill values to prerender.
     * @param {string} placeholder  Placeholder text for the free-text input.
     */
    _renderPillInput(fieldName, values, placeholder) {
        const pills = (values || []).map((v, i) => `
            <span class="metadata-pill" data-pill-index="${i}">
                <span class="metadata-pill-text">${this._escape(v)}</span>
                <button type="button" class="metadata-pill-remove" data-pill-remove="${this._escape(fieldName)}" data-index="${i}" aria-label="Remove">&times;</button>
            </span>
        `).join('');

        const inputId = `meta-${fieldName}-input`;
        return `
            <div class="metadata-pill-input" data-pill-field="${this._escape(fieldName)}">
                <div class="metadata-pill-list" data-pill-list="${this._escape(fieldName)}">${pills}</div>
                <input type="text" id="${inputId}" class="metadata-pill-entry" data-pill-entry="${this._escape(fieldName)}" placeholder="${this._escape(placeholder || '')}" autocomplete="off" />
            </div>
        `;
    },

    /**
     * Re-render pill list for a given field without rebuilding the whole
     * form. Called after any pill add/remove.
     */
    _refreshPillList(fieldName) {
        const list = document.querySelector(`[data-pill-list="${fieldName}"]`);
        if (!list) return;
        const values = this.metadata[fieldName] || [];
        list.innerHTML = values.map((v, i) => `
            <span class="metadata-pill" data-pill-index="${i}">
                <span class="metadata-pill-text">${this._escape(v)}</span>
                <button type="button" class="metadata-pill-remove" data-pill-remove="${this._escape(fieldName)}" data-index="${i}" aria-label="Remove">&times;</button>
            </span>
        `).join('');
        this._bindPillRemoveButtons(fieldName);
    },

    _bindPillRemoveButtons(fieldName) {
        document.querySelectorAll(`[data-pill-remove="${fieldName}"]`).forEach(btn => {
            btn.addEventListener('click', (e) => {
                e.preventDefault();
                const idx = parseInt(btn.getAttribute('data-index'), 10);
                if (Number.isNaN(idx)) return;
                const arr = this.metadata[fieldName] || [];
                arr.splice(idx, 1);
                this.metadata[fieldName] = arr;
                this._refreshPillList(fieldName);
                this._clearStatus();
            });
        });
    },

    _addPill(fieldName, value) {
        const trimmed = (value || '').trim();
        if (!trimmed) return;
        const arr = this.metadata[fieldName] || [];
        // Prevent exact duplicates (case-insensitive)
        const exists = arr.some(v => v.toLowerCase() === trimmed.toLowerCase());
        if (exists) return;
        arr.push(trimmed);
        this.metadata[fieldName] = arr;
        this._refreshPillList(fieldName);
        this._clearStatus();
    },

    _initFormBindings() {
        // Section accordion toggles
        document.querySelectorAll('[data-section-toggle]').forEach(btn => {
            btn.addEventListener('click', () => {
                const key = btn.getAttribute('data-section-toggle');
                const section = document.querySelector(`[data-section="${key}"]`);
                if (!section) return;
                const expanded = section.getAttribute('data-expanded') === 'true';
                section.setAttribute('data-expanded', expanded ? 'false' : 'true');
                const chev = btn.querySelector('.metadata-section-chevron');
                if (chev) chev.innerHTML = expanded ? '&#9654;' : '&#9660;';

                // Phase 4: lazy-load chapter data when expanding the chapters
                // section (or refresh on each re-expand to catch MD changes).
                if (key === 'chapters' && !expanded) {
                    this._loadChapters();
                }
                // Phase 5: refresh raw JSON view on expand
                if (key === 'rawjson' && !expanded) {
                    this._refreshRawJsonView();
                }
            });
        });

        // Per-platform description tabs
        document.querySelectorAll('[data-desc-tab]').forEach(btn => {
            btn.addEventListener('click', () => {
                const tab = btn.getAttribute('data-desc-tab');
                // Toggle active tab button
                document.querySelectorAll('[data-desc-tab]').forEach(b => b.classList.remove('active'));
                btn.classList.add('active');
                // Toggle visible pane
                document.querySelectorAll('[data-desc-pane]').forEach(p => {
                    p.style.display = p.getAttribute('data-desc-pane') === tab ? '' : 'none';
                });
            });
        });

        // Per-platform description textareas — write into metadata.descriptions
        const descShortEl = document.getElementById('meta-desc-short');
        const descAnnouncementEl = document.getElementById('meta-desc-announcement');
        if (descShortEl) {
            descShortEl.addEventListener('input', () => {
                this.metadata.descriptions.short = descShortEl.value;
                this._clearStatus();
                this._updateCharCounter('meta-desc-short', 'meta-desc-short-counter', this.DESC_MAX);
            });
        }
        if (descAnnouncementEl) {
            descAnnouncementEl.addEventListener('input', () => {
                this.metadata.descriptions.announcement = descAnnouncementEl.value;
                this._clearStatus();
                this._updateCharCounter('meta-desc-announcement', 'meta-desc-announcement-counter', this.ANNOUNCEMENT_MAX);
            });
        }

        // Field inputs — live-update this.metadata + counters
        document.querySelectorAll('[data-field]').forEach(el => {
            const field = el.getAttribute('data-field');
            el.addEventListener('input', () => {
                this.metadata[field] = el.value;
                this._clearStatus();
                // Refresh counters for description/summary
                if (field === 'description') {
                    this._updateCharCounter('meta-description', 'meta-desc-counter', this.DESC_MAX);
                } else if (field === 'summary') {
                    this._updateCharCounter('meta-summary', 'meta-summary-counter', this.SUMMARY_MAX);
                }
                // Update drawer subtitle if title changes
                if (field === 'title') {
                    const sub = document.getElementById('metadata-drawer-subtitle');
                    if (sub) sub.textContent = el.value || this.storyName;
                }
            });
            el.addEventListener('change', () => {
                this.metadata[field] = el.value;
            });
        });

        // Classifications — warning/category checkboxes
        document.querySelectorAll('[data-classification]').forEach(cb => {
            cb.addEventListener('change', () => {
                const kind = cb.getAttribute('data-classification');
                const value = cb.value;
                const key = kind === 'warning' ? 'warnings' : 'categories';
                const arr = this.metadata[key] || [];
                const idx = arr.indexOf(value);
                if (cb.checked && idx === -1) arr.push(value);
                else if (!cb.checked && idx !== -1) arr.splice(idx, 1);
                this.metadata[key] = arr;
                this._clearStatus();
                // Clear warnings error banner once user picks anything
                if (key === 'warnings') {
                    const errEl = document.getElementById('meta-error-warnings');
                    if (errEl) errEl.textContent = '';
                }
            });
        });

        // Pill inputs — bind entry fields + existing remove buttons
        document.querySelectorAll('[data-pill-entry]').forEach(input => {
            const field = input.getAttribute('data-pill-entry');
            input.addEventListener('keydown', (e) => {
                if (e.key === 'Enter' || e.key === ',') {
                    e.preventDefault();
                    this._addPill(field, input.value);
                    input.value = '';
                } else if (e.key === 'Backspace' && input.value === '') {
                    // Remove last pill on backspace-in-empty
                    const arr = this.metadata[field] || [];
                    if (arr.length) {
                        arr.pop();
                        this.metadata[field] = arr;
                        this._refreshPillList(field);
                        this._clearStatus();
                    }
                }
            });
            input.addEventListener('blur', () => {
                // Convert any pending text on blur so nothing is silently lost
                if (input.value.trim()) {
                    this._addPill(field, input.value);
                    input.value = '';
                }
            });
        });
        // Bind remove buttons for each pill field currently on screen
        const pillFields = new Set();
        document.querySelectorAll('[data-pill-remove]').forEach(btn => {
            pillFields.add(btn.getAttribute('data-pill-remove'));
        });
        pillFields.forEach(f => this._bindPillRemoveButtons(f));

        // Per-platform tag tabs (Phase 3a)
        document.querySelectorAll('[data-tag-tab]').forEach(btn => {
            btn.addEventListener('click', () => {
                const p = btn.getAttribute('data-tag-tab');
                if (p === this._activeTagPlatform) return;
                this._activeTagPlatform = p;
                this._closeDropdown();
                this._updateTagTabs();
                this._rerenderTagTabBody();
            });
        });
        // Initial tag body bindings (pills + input for active tab)
        this._bindTagTabBodyEvents();

        // Fix spaces in default tags
        document.getElementById('metadata-tag-fix-spaces')?.addEventListener('click', () => {
            this._fixSpacesInTags();
        });
        document.getElementById('metadata-tag-sort-alpha')?.addEventListener('click', () => {
            this._sortTagsAlphabetically();
        });

        // Platform toggle checkboxes
        document.querySelectorAll('[data-platform-toggle]').forEach(cb => {
            cb.addEventListener('change', () => {
                const p = cb.getAttribute('data-platform-toggle');
                this.metadata.platforms[p] = cb.checked;
                this._clearStatus();
            });
        });

        // Phase 5: bind cover + raw JSON once up-front so they work even if
        // the user expands those sections without interacting elsewhere.
        this._bindCoverEvents();
        this._bindRawJsonEvents();
    },

    // ---------------------------------------------------------------------
    // Section 6: Chapters (Phase 4)
    //
    // Lazy-loaded accordion. Each chapter is a collapsed row that expands
    // into a form with override title, description, and simple comma-
    // separated tag textareas per platform (no autocomplete in Phase 4;
    // full pill/autocomplete machinery is Phase 4b).
    // ---------------------------------------------------------------------

    _renderChaptersSection() {
        // The body itself is rendered lazily — we emit a shell here and
        // _renderChapterRows() populates #metadata-chapters-body on expand.
        const count = this._chapterData ? this._chapterData.chapters.length : '...';
        return `
            <section class="metadata-section" data-section="chapters" data-expanded="false">
                <button type="button" class="metadata-section-header" data-section-toggle="chapters">
                    <span class="metadata-section-chevron">&#9654;</span>
                    <span>Chapters <span class="metadata-section-count" id="metadata-chapters-count">(${count})</span></span>
                </button>
                <div class="metadata-section-body">
                    <div id="metadata-chapters-body">
                        <div class="metadata-loading">Expand to load chapters...</div>
                    </div>
                </div>
            </section>
        `;
    },

    async _loadChapters() {
        if (this._chaptersLoading) return;
        this._chaptersLoading = true;
        const host = document.getElementById('metadata-chapters-body');
        if (host && !this._chaptersLoaded) {
            host.innerHTML = `<div class="metadata-loading">Loading chapters...</div>`;
        }
        try {
            const resp = await fetch(`/api/editor/stories/${encodeURIComponent(this.storyName)}/chapters`);
            if (!resp.ok) {
                const txt = await resp.text();
                throw new Error(txt || `HTTP ${resp.status}`);
            }
            const data = await resp.json();
            this._chapterData = data;
            this._chaptersLoaded = true;

            // Sync metadata.chapter_info with detected chapters so edits made
            // via this UI are persisted even if the original story.json had
            // no chapter_info yet. We never clobber user-authored descriptions
            // or tags — we only add stubs for MD chapters that aren't in
            // metadata yet.
            this._syncChapterInfoFromLoad(data);

            this._renderChapterRows();
            const countEl = document.getElementById('metadata-chapters-count');
            if (countEl) countEl.textContent = `(${data.chapters.length})`;
        } catch (err) {
            if (host) {
                host.innerHTML = `<div class="metadata-error-banner">Failed to load chapters: ${this._escape(err.message || err)}</div>`;
            }
        } finally {
            this._chaptersLoading = false;
        }
    },

    /**
     * Upsert detected chapters into this.metadata.chapter_info. The backend
     * response is the source of truth for which chapters exist; we take
     * description/tags from any existing entry so edits survive.
     */
    _syncChapterInfoFromLoad(data) {
        const md = this.metadata;
        const byIndex = new Map();
        (md.chapter_info || []).forEach(entry => {
            if (entry && typeof entry === 'object' && typeof entry.index === 'number') {
                byIndex.set(entry.index, entry);
            }
        });

        const merged = [];
        data.chapters.forEach(ch => {
            const existing = byIndex.get(ch.index) || {};
            const tags = existing.tags && typeof existing.tags === 'object' && !Array.isArray(existing.tags)
                ? existing.tags : {};
            merged.push({
                index: ch.index,
                // Prefer MD title unless user has an override
                title: (typeof existing.title === 'string' && existing.title.trim())
                    ? existing.title
                    : (ch.title_from_md || ch.title || ''),
                words: ch.words,
                description: typeof existing.description === 'string' ? existing.description : '',
                tags: {
                    default: Array.isArray(tags.default) ? tags.default : [],
                    sofurry: Array.isArray(tags.sofurry) ? tags.sofurry : [],
                    wattpad: Array.isArray(tags.wattpad) ? tags.wattpad : [],
                },
            });
        });

        md.chapter_info = merged;
    },

    _renderChapterRows() {
        const host = document.getElementById('metadata-chapters-body');
        if (!host || !this._chapterData) return;

        const { chapters, drift } = this._chapterData;
        const hasDrift = (drift.added_in_md.length + drift.removed_in_md.length + drift.renamed.length) > 0;
        const driftHtml = hasDrift ? this._renderChapterDriftBanner(drift) : '';

        if (!chapters.length) {
            host.innerHTML = driftHtml + `<div class="metadata-loading">No chapters detected in MASTER.md.</div>`;
            return;
        }

        const rows = chapters.map(ch => this._renderChapterRow(ch)).join('');
        host.innerHTML = driftHtml + `<div class="metadata-chapter-list">${rows}</div>`;
        this._bindChapterRowEvents();
    },

    _renderChapterDriftBanner(drift) {
        const bits = [];
        if (drift.added_in_md.length) bits.push(`${drift.added_in_md.length} new chapter${drift.added_in_md.length === 1 ? '' : 's'} in MASTER.md`);
        if (drift.removed_in_md.length) bits.push(`${drift.removed_in_md.length} chapter${drift.removed_in_md.length === 1 ? '' : 's'} removed from MASTER.md`);
        if (drift.renamed.length) bits.push(`${drift.renamed.length} renamed`);
        const label = bits.join(', ');
        return `
            <div class="metadata-chapter-drift-banner">
                <div class="metadata-chapter-drift-text">Chapter drift: ${this._escape(label)}.</div>
                <div class="metadata-chapter-drift-actions">
                    <button type="button" class="btn btn-sm" data-chapter-action="sync-md">Sync from MD</button>
                </div>
            </div>
        `;
    },

    _renderChapterRow(ch) {
        const expanded = this._expandedChapter === ch.index;
        const chev = expanded ? '&#9660;' : '&#9654;';
        const removed = (!ch.in_md && ch.in_metadata);
        const rowCls = `metadata-chapter-row${expanded ? ' metadata-chapter-row-expanded' : ''}${removed ? ' metadata-chapter-row-removed' : ''}`;
        const title = ch.title || ch.title_from_md || `Chapter ${ch.index}`;
        const detail = expanded ? this._renderChapterDetail(ch) : '';
        return `
            <div class="${rowCls}" data-chapter-index="${ch.index}">
                <button type="button" class="metadata-chapter-row-header" data-chapter-toggle="${ch.index}">
                    <span class="metadata-chapter-row-chevron">${chev}</span>
                    <span class="metadata-chapter-row-title">${this._escape(title)}</span>
                    <span class="metadata-chapter-row-meta">${ch.words ? ch.words.toLocaleString() + ' words' : ''}</span>
                </button>
                <div class="metadata-chapter-row-detail">${detail}</div>
            </div>
        `;
    },

    _renderChapterDetail(ch) {
        // Find the live editable entry in metadata.chapter_info
        const entry = this._getChapterEntry(ch.index) || { title: '', description: '', tags: {} };
        const overrideVal = (entry.title && entry.title !== ch.title_from_md) ? entry.title : '';
        const desc = entry.description || '';
        // Phase 4b: use per-chapter active sub-tab (falls back to component default).
        const active = this._chapterTagPlatformByIdx[ch.index] || this._activeChapterTagPlatform || 'default';
        this._chapterTagPlatformByIdx[ch.index] = active;

        const tabs = this._CHAPTER_TAG_PLATFORMS.map(p => {
            const cls = p === active ? ' metadata-chapter-tag-tab-active' : '';
            return `<button type="button" class="metadata-chapter-tag-tab${cls}" data-chapter-tag-tab="${this._escape(p)}" data-chapter-index="${ch.index}">${this._escape(this.PLATFORM_LABELS[p] || p)}</button>`;
        }).join('');

        const removedNote = (!ch.in_md && ch.in_metadata) ? `
            <div class="metadata-chapter-removed-note">
                This chapter exists in metadata but not in MASTER.md.
                <button type="button" class="btn btn-sm btn-outline" data-chapter-action="remove" data-chapter-index="${ch.index}">Remove from metadata</button>
            </div>
        ` : '';

        const mdTitleLine = ch.title_from_md
            ? `<div class="metadata-chapter-md-title">MASTER.md title: <code>${this._escape(ch.title_from_md)}</code></div>`
            : '';

        // Phase 4b: render the pill+autocomplete tag UI for the active
        // chapter sub-tab (Default / SoFurry / Wattpad). Uses per-chapter
        // input IDs so the dropdown portal can position against the right
        // input when multiple chapter rows are expanded simultaneously
        // (only one is actually expanded at a time, but IDs must still be
        // unique against the story-level input).
        const tagBodyHtml = this._renderChapterTagBody(ch.index, active);

        return `
            <div class="metadata-chapter-detail">
                ${removedNote}
                ${mdTitleLine}
                <div class="metadata-field">
                    <label for="meta-chapter-title-${ch.index}">Override title <span class="metadata-hint">(leave empty to use MD title)</span></label>
                    <input type="text" id="meta-chapter-title-${ch.index}" data-chapter-field="title" data-chapter-index="${ch.index}" value="${this._escape(overrideVal)}" placeholder="${this._escape(ch.title_from_md || '')}" autocomplete="off" />
                </div>
                <div class="metadata-field">
                    <label for="meta-chapter-desc-${ch.index}">Description</label>
                    <textarea id="meta-chapter-desc-${ch.index}" data-chapter-field="description" data-chapter-index="${ch.index}" rows="3">${this._escape(desc)}</textarea>
                </div>
                <div class="metadata-field metadata-chapter-thumb-field">
                    <label>Chapter thumbnail <span class="metadata-hint">(optional, falls back to story cover)</span></label>
                    <div class="metadata-chapter-thumb-row">
                        ${this._renderChapterThumb(ch.index)}
                        <label class="btn btn-xs btn-outline" for="meta-chapter-thumb-${ch.index}">Upload</label>
                        <input type="file" id="meta-chapter-thumb-${ch.index}" data-chapter-thumb="${ch.index}" accept="image/png,image/jpeg,image/webp" style="display:none" />
                    </div>
                </div>
                <div class="metadata-field">
                    <label>Per-platform tags <span class="metadata-hint">(independent of story tags)</span></label>
                    <div class="metadata-chapter-tag-tabs" role="tablist">${tabs}</div>
                    <div class="metadata-chapter-tag-body" id="metadata-chapter-tag-body-${ch.index}">
                        ${tagBodyHtml}
                    </div>
                </div>
            </div>
        `;
    },

    /**
     * Phase 4b: body of a single chapter's tag sub-tab. Emits pill list +
     * autocomplete input + tag count, modelled after _renderTagTabBody
     * but writing to metadata.chapter_info[idx].tags[platform].
     */
    _renderChapterTagBody(chapterIdx, platform) {
        const entry = this._getChapterEntry(chapterIdx) || { tags: {} };
        const tags = (entry.tags && Array.isArray(entry.tags[platform])) ? entry.tags[platform] : [];
        const aliases = this._tagDb ? this._tagDb.aliases : {};
        const byName = this._tagDb ? this._tagDb.byName : null;

        const pills = tags.map((t, i) => {
            const inDb = byName ? byName.has(t) : false;
            const aliasTarget = (!inDb && aliases[t]) ? aliases[t] : null;
            let cls = 'metadata-tag-pill';
            if (byName && !inDb && !aliasTarget) cls += ' metadata-tag-pill-unknown';
            const aliasNote = aliasTarget ? `<span class="metadata-tag-pill-alias">&rarr; ${this._escape(aliasTarget)}</span>` : '';
            return `
                <span class="${cls}" data-tag-pill-index="${i}">
                    <span class="metadata-tag-pill-text">${this._escape(t)}</span>
                    ${aliasNote}
                    <button type="button" class="metadata-tag-pill-remove" data-chapter-tag-remove="${this._escape(platform)}" data-chapter-index="${chapterIdx}" data-index="${i}" aria-label="Remove tag">&times;</button>
                </span>
            `;
        }).join('');

        const limit = this.TAG_LIMITS[platform];
        const limitLabel = (limit === Infinity) ? '&infin;' : limit;
        const overLimit = (limit !== Infinity) && tags.length > limit;
        const inputId = `metadata-tag-input-chapter-${chapterIdx}`;

        return `
            <div class="metadata-tag-pills" id="metadata-chapter-tag-pills-${chapterIdx}-${this._escape(platform)}">${pills}</div>
            <div class="metadata-tag-input-wrap">
                <input type="text"
                       class="metadata-tag-input"
                       id="${inputId}"
                       data-chapter-tag-input="${chapterIdx}"
                       data-chapter-tag-platform-input="${this._escape(platform)}"
                       placeholder="Add tag..."
                       autocomplete="off" />
            </div>
            <div class="metadata-tag-count ${overLimit ? 'metadata-tag-count-over' : ''}">
                <span>${tags.length} tags</span>
                <span class="metadata-tag-count-sep">&middot;</span>
                <span>Platform max: ${limitLabel}</span>
            </div>
        `;
    },

    /**
     * Phase 4b: re-render a single chapter's expanded detail view (used
     * after add/remove inside a chapter's tag list, so we don't collapse
     * the row or rebuild the whole chapter list).
     */
    _rerenderChapterDetail(chapterIdx) {
        const host = document.getElementById('metadata-chapters-body');
        if (!host) return;
        const rowEl = host.querySelector(`[data-chapter-index="${chapterIdx}"]`);
        if (!rowEl) return;
        const detailHost = rowEl.querySelector('.metadata-chapter-row-detail');
        if (!detailHost) return;

        // Look up the chapter descriptor to rebuild
        const ch = this._chapterData && this._chapterData.chapters.find(c => c.index === chapterIdx);
        if (!ch) return;
        detailHost.innerHTML = this._renderChapterDetail(ch);
        // Rebind chapter-scoped events on the newly-inserted nodes.
        this._bindChapterDetailEvents(chapterIdx);
    },

    /**
     * Phase 4b: re-render just the tag body portion of a chapter detail
     * when the user switches the sub-tab or adds/removes a pill. Keeps
     * input focus + text inputs (title/description) untouched.
     */
    _rerenderChapterTagBody(chapterIdx) {
        const body = document.getElementById(`metadata-chapter-tag-body-${chapterIdx}`);
        if (!body) return;
        const platform = this._chapterTagPlatformByIdx[chapterIdx] || 'default';
        body.innerHTML = this._renderChapterTagBody(chapterIdx, platform);
        this._bindChapterTagInputEvents(chapterIdx);
        this._bindChapterTagPillRemoveEvents(chapterIdx);
    },

    _getChapterEntry(index) {
        return (this.metadata.chapter_info || []).find(e => e && e.index === index) || null;
    },

    _bindChapterRowEvents() {
        const host = document.getElementById('metadata-chapters-body');
        if (!host) return;

        // Row header toggles
        host.querySelectorAll('[data-chapter-toggle]').forEach(btn => {
            btn.addEventListener('click', () => {
                const idx = parseInt(btn.getAttribute('data-chapter-toggle'), 10);
                if (Number.isNaN(idx)) return;
                // Close any open autocomplete dropdown when collapsing a row
                if (this._tagDropdownContext && this._tagDropdownContext.scope === 'chapter') {
                    this._closeDropdown();
                }
                this._expandedChapter = (this._expandedChapter === idx) ? null : idx;
                this._renderChapterRows();
            });
        });

        // Drift banner actions
        host.querySelectorAll('[data-chapter-action]').forEach(btn => {
            const action = btn.getAttribute('data-chapter-action');
            btn.addEventListener('click', (e) => {
                e.preventDefault();
                if (action === 'sync-md') {
                    if (this._chapterData) this._syncChapterInfoFromLoad(this._chapterData);
                    this._loadChapters();
                } else if (action === 'remove') {
                    const idx = parseInt(btn.getAttribute('data-chapter-index'), 10);
                    if (Number.isNaN(idx)) return;
                    this.metadata.chapter_info = (this.metadata.chapter_info || []).filter(e => !(e && e.index === idx));
                    if (this._chapterData) {
                        this._chapterData.chapters = this._chapterData.chapters.filter(c => c.index !== idx);
                        this._chapterData.drift.removed_in_md = this._chapterData.drift.removed_in_md.filter(d => d.index !== idx);
                    }
                    if (this._expandedChapter === idx) this._expandedChapter = null;
                    this._clearStatus();
                    this._renderChapterRows();
                }
            });
        });

        // Bind per-chapter detail events for whichever chapter is expanded.
        if (this._expandedChapter != null) {
            this._bindChapterDetailEvents(this._expandedChapter);
        }
    },

    /**
     * Phase 4b: bind all interactive elements inside a single chapter's
     * expanded detail (override title, description, tag sub-tabs, tag
     * pills, tag autocomplete input). Called on initial render, re-render
     * after sub-tab switch, and after pill add/remove.
     */
    _bindChapterDetailEvents(chapterIdx) {
        const host = document.getElementById('metadata-chapters-body');
        if (!host) return;
        const rowEl = host.querySelector(`[data-chapter-index="${chapterIdx}"]`);
        if (!rowEl) return;

        // Title + description fields (not tags — those use the pill UI)
        rowEl.querySelectorAll('[data-chapter-field]').forEach(el => {
            const field = el.getAttribute('data-chapter-field');
            const idx = parseInt(el.getAttribute('data-chapter-index'), 10);
            if (Number.isNaN(idx)) return;
            if (field !== 'title' && field !== 'description') return;
            const write = () => {
                const entry = this._ensureChapterEntry(idx);
                if (field === 'title') {
                    const v = el.value.trim();
                    const chInfo = (this._chapterData && this._chapterData.chapters.find(c => c.index === idx));
                    const mdTitle = chInfo ? (chInfo.title_from_md || '') : '';
                    entry.title = v || mdTitle;
                } else if (field === 'description') {
                    entry.description = el.value;
                }
                this._clearStatus();
            };
            el.addEventListener('input', write);
            el.addEventListener('change', write);
        });

        // Chapter thumbnail upload
        rowEl.querySelectorAll('[data-chapter-thumb]').forEach(inp => {
            inp.addEventListener('change', (e) => {
                const idx = parseInt(inp.getAttribute('data-chapter-thumb'), 10);
                const f = e.target.files?.[0];
                if (f && !Number.isNaN(idx)) this._uploadChapterThumb(idx, f);
                inp.value = '';
            });
        });

        // Sub-tab clicks — switch active platform for THIS chapter only.
        rowEl.querySelectorAll('[data-chapter-tag-tab]').forEach(btn => {
            btn.addEventListener('click', (e) => {
                e.stopPropagation();
                e.preventDefault();
                const p = btn.getAttribute('data-chapter-tag-tab');
                if (p === this._chapterTagPlatformByIdx[chapterIdx]) return;
                this._chapterTagPlatformByIdx[chapterIdx] = p;
                // Update the stored "default" so the next newly-expanded
                // chapter inherits the user's preference.
                this._activeChapterTagPlatform = p;
                // Close dropdown if it was open on the previous tab's input.
                if (this._tagDropdownContext && this._tagDropdownContext.scope === 'chapter'
                    && this._tagDropdownContext.chapterIdx === chapterIdx) {
                    this._closeDropdown();
                }
                // Update tab active state without rebuilding
                rowEl.querySelectorAll('[data-chapter-tag-tab]').forEach(b => {
                    const bp = b.getAttribute('data-chapter-tag-tab');
                    b.classList.toggle('metadata-chapter-tag-tab-active', bp === p);
                });
                this._rerenderChapterTagBody(chapterIdx);
            });
        });

        // Tag pill remove + autocomplete input
        this._bindChapterTagPillRemoveEvents(chapterIdx);
        this._bindChapterTagInputEvents(chapterIdx);
    },

    _bindChapterTagPillRemoveEvents(chapterIdx) {
        const host = document.getElementById('metadata-chapters-body');
        if (!host) return;
        const rowEl = host.querySelector(`[data-chapter-index="${chapterIdx}"]`);
        if (!rowEl) return;
        rowEl.querySelectorAll('[data-chapter-tag-remove]').forEach(btn => {
            btn.addEventListener('click', (e) => {
                e.preventDefault();
                const platform = btn.getAttribute('data-chapter-tag-remove');
                const idx = parseInt(btn.getAttribute('data-index'), 10);
                if (Number.isNaN(idx)) return;
                this._removeTagFromChapter(chapterIdx, platform, idx);
            });
        });
    },

    _bindChapterTagInputEvents(chapterIdx) {
        const inputId = `metadata-tag-input-chapter-${chapterIdx}`;
        const input = document.getElementById(inputId);
        if (!input) return;
        const platform = input.getAttribute('data-chapter-tag-platform-input');
        const ctx = { scope: 'chapter', platform, chapterIdx, inputId };

        input.addEventListener('focus', () => {
            this._openDropdownFor(platform, input.value, ctx);
        });

        input.addEventListener('input', () => {
            if (platform === 'default' || platform === 'furaffinity' || platform === 'weasyl' || platform === 'itaku') {
                const pos = input.selectionStart;
                const fixed = input.value.replace(/ /g, '_');
                if (fixed !== input.value) {
                    input.value = fixed;
                    input.selectionStart = input.selectionEnd = pos;
                }
            }
            this._openDropdownFor(platform, input.value, ctx);
        });

        input.addEventListener('keydown', (e) => {
            const results = this._tagDropdownResults;
            if (e.key === 'ArrowDown') {
                e.preventDefault();
                if (!results.length) return;
                this._tagDropdownIndex = Math.min(results.length - 1, this._tagDropdownIndex + 1);
                this._renderDropdown(results, input.value);
            } else if (e.key === 'ArrowUp') {
                e.preventDefault();
                if (!results.length) return;
                this._tagDropdownIndex = Math.max(0, this._tagDropdownIndex - 1);
                this._renderDropdown(results, input.value);
            } else if (e.key === 'Enter') {
                e.preventDefault();
                const picked = results[this._tagDropdownIndex];
                if (picked) {
                    this._addTagToChapter(chapterIdx, platform, picked.tag.name);
                } else if (input.value.trim()) {
                    this._addTagToChapter(chapterIdx, platform, input.value);
                }
            } else if (e.key === 'Escape') {
                this._closeDropdown();
                input.blur();
            } else if (e.key === 'Backspace' && input.value === '') {
                const entry = this._getChapterEntry(chapterIdx);
                const tags = entry && entry.tags && Array.isArray(entry.tags[platform]) ? entry.tags[platform] : [];
                if (tags.length) {
                    this._removeTagFromChapter(chapterIdx, platform, tags.length - 1);
                }
            }
        });

        input.addEventListener('blur', () => {
            setTimeout(() => {
                // Only close if context still belongs to this input — avoids
                // fighting with a freshly-opened story-level dropdown.
                const ctx2 = this._tagDropdownContext;
                if (ctx2 && ctx2.scope === 'chapter' && ctx2.chapterIdx === chapterIdx) {
                    this._closeDropdown();
                }
            }, 150);
        });

        // Ensure portal-level handlers exist (no-op after first call)
        this._ensureDropdownPortalHandlers();
    },

    _ensureChapterEntry(index) {
        this.metadata.chapter_info = this.metadata.chapter_info || [];
        let entry = this.metadata.chapter_info.find(e => e && e.index === index);
        if (!entry) {
            entry = { index, title: '', description: '', tags: { default: [], sofurry: [], inkbunny: [], wattpad: [] }, words: 0 };
            this.metadata.chapter_info.push(entry);
            this.metadata.chapter_info.sort((a, b) => a.index - b.index);
        }
        return entry;
    },

    // ---------------------------------------------------------------------
    _renderChapterThumb(chIdx) {
        const thumbs = this.metadata.images?.chapter_thumbnails || {};
        const file = thumbs[String(chIdx)] || '';
        if (!file) return '<span class="metadata-hint">None</span>';
        return `<code class="metadata-chapter-thumb-name">${this._escape(file)}</code>`;
    },

    async _uploadChapterThumb(chIdx, file) {
        if (!file || file.size > 5 * 1024 * 1024) {
            this._setStatus('Thumbnail must be under 5 MB', 'error');
            return;
        }
        const formData = new FormData();
        formData.append('file', file);
        formData.append('chapter_index', String(chIdx));
        try {
            const resp = await fetch(
                `/api/editor/stories/${encodeURIComponent(this.storyName)}/chapter-thumbnail`,
                { method: 'POST', body: formData },
            );
            const data = await resp.json();
            if (data.ok) {
                if (!this.metadata.images) this.metadata.images = {};
                if (!this.metadata.images.chapter_thumbnails) this.metadata.images.chapter_thumbnails = {};
                this.metadata.images.chapter_thumbnails[String(chIdx)] = data.filename;
                // Snapshot the new on-disk write into initialMetadata so
                // the dirty check doesn't flag the upload as a pending
                // edit, and bump lastMtime so the next Save doesn't 409.
                if (typeof data.last_modified === 'number') {
                    this.lastMtime = data.last_modified;
                }
                if (this.initialMetadata) {
                    if (!this.initialMetadata.images) this.initialMetadata.images = {};
                    if (!this.initialMetadata.images.chapter_thumbnails) {
                        this.initialMetadata.images.chapter_thumbnails = {};
                    }
                    this.initialMetadata.images.chapter_thumbnails[String(chIdx)] = data.filename;
                }
                this._setStatus(`Chapter ${chIdx} thumbnail uploaded`, 'success');
                this._rerenderChapterDetail(chIdx);
            } else {
                this._setStatus(data.detail || 'Upload failed', 'error');
            }
        } catch (e) {
            this._setStatus('Upload failed: ' + e.message, 'error');
        }
    },

    // Section 7: Cover Image (Phase 5)
    // ---------------------------------------------------------------------

    _renderCoverSection() {
        return `
            <section class="metadata-section" data-section="cover" data-expanded="false">
                <button type="button" class="metadata-section-header" data-section-toggle="cover">
                    <span class="metadata-section-chevron">&#9654;</span>
                    <span>Cover Image</span>
                </button>
                <div class="metadata-section-body">
                    <div class="metadata-cover-wrap" id="metadata-cover-wrap">
                        ${this._renderCoverBody()}
                    </div>
                </div>
            </section>
        `;
    },

    _renderCoverBody() {
        const hasCover = !!this._coverFilename;
        const src = hasCover
            ? `/api/editor/stories/${encodeURIComponent(this.storyName)}/cover?t=${this._coverBustKey}`
            : '';
        const preview = hasCover
            ? `<img class="metadata-cover-thumb" src="${src}" alt="Cover preview" data-img-fallback /><div class="metadata-cover-empty" style="display:none;">Preview unavailable</div>`
            : `<div class="metadata-cover-empty">No cover image</div>`;
        const meta = hasCover
            ? `<div class="metadata-cover-meta"><code>${this._escape(this._coverFilename)}</code></div>`
            : '';
        return `
            <div class="metadata-cover-preview" id="metadata-cover-preview">
                ${preview}
            </div>
            ${meta}
            <div class="metadata-cover-actions">
                <label class="btn btn-sm metadata-cover-upload-btn" for="metadata-cover-file">
                    ${hasCover ? 'Replace image' : 'Upload image'}
                </label>
                <input type="file" id="metadata-cover-file" accept="image/png,image/jpeg,image/jpg,image/webp" style="display:none" />
                <span class="metadata-hint">or drop an image on the preview above (PNG/JPG/WebP, max 5MB)</span>
            </div>
            <div class="metadata-cover-status" id="metadata-cover-status"></div>
        `;
    },

    _refreshCoverBody() {
        const wrap = document.getElementById('metadata-cover-wrap');
        if (!wrap) return;
        wrap.innerHTML = this._renderCoverBody();
        this._bindCoverEvents();
    },

    _bindCoverEvents() {
        const fileInput = document.getElementById('metadata-cover-file');
        if (fileInput) {
            fileInput.addEventListener('change', (e) => {
                const f = e.target.files && e.target.files[0];
                if (f) this._uploadCover(f);
                // reset so same file can be re-selected later
                fileInput.value = '';
            });
        }
        const drop = document.getElementById('metadata-cover-preview');
        if (drop) {
            ['dragenter', 'dragover'].forEach(evt => {
                drop.addEventListener(evt, (e) => {
                    e.preventDefault();
                    e.stopPropagation();
                    drop.classList.add('metadata-cover-dropzone-active');
                });
            });
            ['dragleave', 'drop'].forEach(evt => {
                drop.addEventListener(evt, (e) => {
                    e.preventDefault();
                    e.stopPropagation();
                    drop.classList.remove('metadata-cover-dropzone-active');
                });
            });
            drop.addEventListener('drop', (e) => {
                const f = e.dataTransfer && e.dataTransfer.files && e.dataTransfer.files[0];
                if (f) this._uploadCover(f);
            });
        }
    },

    async _uploadCover(file) {
        if (this._coverUploading) return;
        const statusEl = document.getElementById('metadata-cover-status');
        if (!/^image\/(png|jpe?g|webp)$/i.test(file.type) && !/\.(png|jpe?g|webp)$/i.test(file.name)) {
            if (statusEl) {
                statusEl.textContent = 'Unsupported file type. Use PNG, JPG, or WebP.';
                statusEl.className = 'metadata-cover-status metadata-cover-status-error';
            }
            return;
        }
        if (file.size > 5 * 1024 * 1024) {
            if (statusEl) {
                statusEl.textContent = `File too large (${(file.size / 1024 / 1024).toFixed(2)} MB — max 5 MB).`;
                statusEl.className = 'metadata-cover-status metadata-cover-status-error';
            }
            return;
        }
        this._coverUploading = true;
        if (statusEl) {
            statusEl.textContent = 'Uploading...';
            statusEl.className = 'metadata-cover-status';
        }
        try {
            const fd = new FormData();
            fd.append('file', file, file.name);
            const resp = await fetch(`/api/editor/stories/${encodeURIComponent(this.storyName)}/cover`, {
                method: 'POST',
                body: fd,
            });
            if (!resp.ok) {
                const txt = await resp.text();
                throw new Error(txt || `HTTP ${resp.status}`);
            }
            const data = await resp.json();
            // Update metadata so Save persists the filename
            if (!this.metadata.images || typeof this.metadata.images !== 'object' || Array.isArray(this.metadata.images)) {
                this.metadata.images = {};
            }
            this.metadata.images.cover = data.filename;
            this._coverFilename = data.filename;
            this._coverBustKey = Date.now();
            this._clearStatus();
            this._refreshCoverBody();
            const newStatusEl = document.getElementById('metadata-cover-status');
            if (newStatusEl) {
                newStatusEl.textContent = `Uploaded ${data.filename} (${(data.size / 1024).toFixed(1)} KB) — remember to Save to persist filename change.`;
                newStatusEl.className = 'metadata-cover-status metadata-cover-status-ok';
            }
        } catch (err) {
            if (statusEl) {
                statusEl.textContent = `Upload failed: ${err.message || err}`;
                statusEl.className = 'metadata-cover-status metadata-cover-status-error';
            }
        } finally {
            this._coverUploading = false;
        }
    },

    // ---------------------------------------------------------------------
    // Section 8: Raw JSON (Phase 5)
    // ---------------------------------------------------------------------

    _renderRawJsonSection() {
        return `
            <section class="metadata-section" data-section="rawjson" data-expanded="false">
                <button type="button" class="metadata-section-header" data-section-toggle="rawjson">
                    <span class="metadata-section-chevron">&#9654;</span>
                    <span>Raw JSON <span class="metadata-hint">(advanced)</span></span>
                </button>
                <div class="metadata-section-body">
                    <div id="metadata-rawjson-wrap">
                        ${this._renderRawJsonBody()}
                    </div>
                </div>
            </section>
        `;
    },

    _renderRawJsonBody() {
        const pretty = this._safeStringifyMetadata();
        const editing = this._rawJsonEditMode;
        const warning = editing
            ? `<div class="metadata-rawjson-warning">Direct JSON editing bypasses validation. Use carefully. Click Apply to replace the entire metadata object.</div>`
            : '';
        const ta = `<textarea class="metadata-rawjson-textarea" id="metadata-rawjson-textarea" rows="16" ${editing ? '' : 'readonly'}>${this._escape(pretty)}</textarea>`;
        const actions = editing
            ? `
                <div class="metadata-rawjson-actions">
                    <button type="button" class="btn btn-sm" id="metadata-rawjson-apply">Apply</button>
                    <button type="button" class="btn btn-sm btn-outline" id="metadata-rawjson-cancel">Cancel</button>
                </div>
                <div class="metadata-rawjson-error" id="metadata-rawjson-error"></div>
              `
            : `
                <div class="metadata-rawjson-actions">
                    <button type="button" class="btn btn-sm btn-outline" id="metadata-rawjson-edit">Edit JSON</button>
                    <button type="button" class="btn btn-sm btn-outline" id="metadata-rawjson-refresh">Refresh</button>
                </div>
              `;
        return `${warning}${ta}${actions}`;
    },

    _safeStringifyMetadata() {
        try {
            return JSON.stringify(this.metadata, null, 2);
        } catch (err) {
            return `// JSON stringify failed: ${err.message || err}`;
        }
    },

    _refreshRawJsonView() {
        const wrap = document.getElementById('metadata-rawjson-wrap');
        if (!wrap) return;
        wrap.innerHTML = this._renderRawJsonBody();
        this._bindRawJsonEvents();
    },

    _bindRawJsonEvents() {
        const editBtn = document.getElementById('metadata-rawjson-edit');
        if (editBtn) {
            editBtn.addEventListener('click', () => {
                this._rawJsonEditMode = true;
                this._refreshRawJsonView();
                const ta = document.getElementById('metadata-rawjson-textarea');
                if (ta) ta.focus();
            });
        }
        const refreshBtn = document.getElementById('metadata-rawjson-refresh');
        if (refreshBtn) {
            refreshBtn.addEventListener('click', () => this._refreshRawJsonView());
        }
        const applyBtn = document.getElementById('metadata-rawjson-apply');
        if (applyBtn) {
            applyBtn.addEventListener('click', () => this._applyRawJsonEdit());
        }
        const cancelBtn = document.getElementById('metadata-rawjson-cancel');
        if (cancelBtn) {
            cancelBtn.addEventListener('click', () => {
                this._rawJsonEditMode = false;
                this._refreshRawJsonView();
            });
        }
    },

    _applyRawJsonEdit() {
        const ta = document.getElementById('metadata-rawjson-textarea');
        const errEl = document.getElementById('metadata-rawjson-error');
        if (!ta) return;
        let parsed;
        try {
            parsed = JSON.parse(ta.value);
        } catch (err) {
            if (errEl) errEl.textContent = `Parse error: ${err.message || err}`;
            return;
        }
        if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) {
            if (errEl) errEl.textContent = 'Root must be a JSON object.';
            return;
        }
        if (!confirm('Replace the entire metadata object with the edited JSON? All form state will be re-rendered from this.')) {
            return;
        }
        this.metadata = parsed;
        this._normaliseMetadata();
        this._rawJsonEditMode = false;
        // Full re-render so all form fields reflect the new state
        this._renderForm();
        this._initFormBindings();
        // Refresh cover preview binding (rendered as part of _renderForm)
        this._refreshCoverBody();
        this._clearStatus();
        this._setStatus('JSON applied (not yet saved)', 'info');
    },

    _updateCharCounter(inputId, counterId, max) {
        const input = document.getElementById(inputId);
        const counter = document.getElementById(counterId);
        if (!input || !counter) return;
        const len = (input.value || '').length;
        counter.textContent = `${len}/${max}`;
        counter.classList.toggle('metadata-char-counter-over', len > max);
    },

    // ---------------------------------------------------------------------
    // Validation
    // ---------------------------------------------------------------------

    _validate() {
        const errors = [];
        // Clear previous errors
        document.querySelectorAll('.metadata-error').forEach(el => { el.textContent = ''; });

        const title = (this.metadata.title || '').trim();
        if (!title) {
            errors.push({ field: 'title', msg: 'Title is required.' });
        }

        const rating = this.metadata.rating;
        if (rating && rating !== '') {
            const valid = this.RATINGS.some(r => r.toLowerCase() === rating.toString().toLowerCase());
            if (!valid) {
                errors.push({ field: 'rating', msg: `Rating must be one of: ${this.RATINGS.join(', ')}` });
            }
        }

        // Tier 1: at least one archive warning required (AO3 standard)
        const warnings = Array.isArray(this.metadata.warnings) ? this.metadata.warnings : [];
        if (warnings.length === 0) {
            errors.push({ field: 'warnings', msg: 'Select at least one archive warning (AO3 standard).' });
            // Also auto-expand the Classifications section so user can see it
            const sec = document.querySelector('[data-section="classifications"]');
            if (sec && sec.getAttribute('data-expanded') !== 'true') {
                sec.setAttribute('data-expanded', 'true');
                const chev = sec.querySelector('.metadata-section-chevron');
                if (chev) chev.innerHTML = '&#9660;';
            }
        }

        // Render errors inline
        errors.forEach(e => {
            const el = document.getElementById(`meta-error-${e.field}`);
            if (el) el.textContent = e.msg;
        });

        return errors;
    },

    // ---------------------------------------------------------------------
    // Save
    // ---------------------------------------------------------------------

    async save() {
        const errors = this._validate();
        if (errors.length) {
            // Scroll to first errored field (or the error banner if the
            // field is a checkbox group like warnings with no single input)
            const firstField = errors[0].field;
            let target = document.getElementById(`meta-${firstField}`);
            if (!target) {
                target = document.getElementById(`meta-error-${firstField}`)
                    || document.getElementById(`meta-${firstField}-list`);
            }
            if (target) {
                if (typeof target.focus === 'function') target.focus();
                target.scrollIntoView({ behavior: 'smooth', block: 'center' });
            }
            this._setStatus('Fix errors before saving', 'error');
            return;
        }

        this._setStatus('Saving...', 'info');
        try {
            const resp = await fetch(`/api/editor/stories/${encodeURIComponent(this.storyName)}/metadata`, {
                method: 'PUT',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    metadata: this.metadata,
                    expected_mtime: this.lastMtime,
                }),
            });
            if (resp.status === 409) {
                if (confirm('Story metadata changed externally — reload?')) {
                    await this._loadMetadata();
                    this._renderForm();
                    this._initFormBindings();
                    this._setStatus('Reloaded', 'info');
                } else {
                    this._setStatus('Save aborted (conflict)', 'error');
                }
                return;
            }
            if (!resp.ok) {
                const txt = await resp.text();
                this._setStatus(`Save failed: ${txt}`, 'error');
                return;
            }
            const data = await resp.json();
            this.lastMtime = data.last_modified || this.lastMtime;
            // Snapshot new clean state
            this.initialMetadata = JSON.parse(JSON.stringify(this.metadata));
            this._setStatus('Saved', 'ok');
        } catch (err) {
            this._setStatus(`Save failed: ${err.message || err}`, 'error');
        }
    },

    _setStatus(msg, kind) {
        const el = document.getElementById('metadata-drawer-status');
        if (!el) return;
        el.textContent = msg;
        el.className = 'metadata-drawer-status';
        if (kind) el.classList.add(`metadata-drawer-status-${kind}`);
    },

    _clearStatus() {
        const el = document.getElementById('metadata-drawer-status');
        if (el && el.classList.contains('metadata-drawer-status-ok')) {
            // Clear "Saved" once user starts editing again
            el.textContent = '';
            el.className = 'metadata-drawer-status';
        }
    },

    // ---------------------------------------------------------------------
    // Dirty check
    // ---------------------------------------------------------------------

    _isDirty() {
        if (!this.metadata || !this.initialMetadata) return false;
        try {
            return JSON.stringify(this.metadata) !== JSON.stringify(this.initialMetadata);
        } catch (_) {
            return false;
        }
    },

    // ---------------------------------------------------------------------
    // Helpers
    // ---------------------------------------------------------------------

    _escape(s) {
        if (s === null || s === undefined) return '';
        return String(s)
            .replace(/&/g, '&amp;')
            .replace(/</g, '&lt;')
            .replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;')
            .replace(/'/g, '&#39;');
    },
};

// Expose globally so editor.js (and users) can reach it
window.MetaEditor = MetaEditor;
