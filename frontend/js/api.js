/* ── API fetch wrapper with error handling ─────────────────── */
/*
 * API — singleton object that centralises all backend communication.
 *
 * Every method returns a Promise that resolves to parsed JSON on success
 * or rejects with an Error whose message includes the HTTP status and
 * response body text.  Two core methods (get / post) handle the actual
 * fetch; every other property is a thin convenience wrapper that calls
 * one of those two so callers never need to remember endpoint paths.
 *
 * Usage:  const data = await API.getSubmissions({ page: 1, limit: 20 });
 */

// Debug logging — enable via: localStorage.setItem('pawpoller_debug', '1')
const _API_DEBUG = localStorage.getItem('pawpoller_debug') === '1';

/* Auth-failure modal escalation (2.54.0). When a POST for an action the user
 * just triggered (post / publish / upload) fails because a platform session
 * expired, pop the blocking error modal in addition to the caller's own error
 * handling. Kept narrow to avoid false positives: only certain statuses, only
 * action-ish paths, and only messages that clearly name a session/cookie issue
 * (so the credential-connect flow — which shows its own inline message — and
 * ordinary validation errors don't trigger it). */
function _isActionPath(path) {
    return /\/(post|publish|posting|posts|upload)/i.test(path || '')
        && !/\/(connect|validate)/i.test(path || '');
}
function _looksLikeSessionAuthError(status, text) {
    if (![401, 403, 502].includes(status)) return false;
    return /session (expired|invalid)|re-?(copy|paste)[^.]*cookie|cookie validation failed|not (authenticated|logged ?in)|log ?in failed|access token (expired|invalid)/i
        .test(text || '');
}
function _cleanErr(text) {
    // FastAPI HTTPException bodies are {"detail": "..."}. Show just the detail.
    try {
        const j = JSON.parse(text);
        if (j && typeof j.detail === 'string') return j.detail;
    } catch (e) { /* not JSON — use as-is */ }
    return String(text || '').slice(0, 300);
}
function _maybeAuthModal(path, status, text) {
    if (!window.errorModal) return false;
    if (!_isActionPath(path) || !_looksLikeSessionAuthError(status, text)) return false;
    window.errorModal({
        title: 'Action failed — session expired',
        message: _cleanErr(text),
        actionLabel: 'Fix in Settings',
        actionHref: '#/settings/platforms',
    });
    return true;
}

/* Achievement-style error card (2.159.0, error_popup.js). Every failed
 * MUTATING request surfaces one — with copy-report + send-to-dev — unless the
 * auth modal above already took it. GETs are deliberately not hooked: screens
 * poll and prefetch constantly, and a flaky read shouldn't pop a card. */
function _popError(method, path, status, text) {
    if (window.ErrorPopup) window.ErrorPopup.onApiError(method, path, status, text);
}

const API = {

    /* ── Core transport: GET ────────────────────────────────────
     * Builds a fully-qualified URL from `path` and optional `params`,
     * stripping any null or empty-string values so the backend never
     * receives "?key=" noise.  Returns the parsed JSON body on a 2xx
     * response; throws an Error for network failures or non-OK status.
     */
    async get(path, params = {}) {
        const url = new URL(path, window.location.origin);
        Object.entries(params).forEach(([k, v]) => {
            if (v != null && v !== '') url.searchParams.set(k, v);
        });
        if (_API_DEBUG) console.log('[API] GET', url.toString());
        let resp;
        try {
            resp = await fetch(url);
        } catch (err) {
            console.error('[API] Network error:', err);
            throw new Error(`Network error: ${err.message}`);
        }
        if (!resp.ok) {
            const text = await resp.text();
            console.error(`[API] ${resp.status} on GET ${path}:`, text);
            throw new Error(`API ${resp.status}: ${text}`);
        }
        const data = await resp.json();
        if (_API_DEBUG) console.log('[API] Response:', path, data);
        return data;
    },

    /* ── Core transport: POST ───────────────────────────────────
     * Sends `body` as a JSON-encoded payload with the appropriate
     * Content-Type header.  Same error-handling pattern as get():
     * throws on network failure or non-OK HTTP status.
     */
    async post(path, body = {}, opts = {}) {
        if (_API_DEBUG) console.log('[API] POST', path, body);
        let resp;
        try {
            resp = await fetch(path, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(body),
            });
        } catch (err) {
            console.error('[API] Network error:', err);
            _popError('POST', path, 0, err.message);
            throw new Error(`Network error: ${err.message}`);
        }
        if (!resp.ok) {
            const text = await resp.text();
            console.error(`[API] ${resp.status} on POST ${path}:`, text);
            // If an action the user just triggered (a post / upload) died on an
            // expired platform session, escalate to a blocking modal — a toast
            // is too easy to miss for something that needs credentials re-entered.
            // opts.quiet: statuses the CALLER handles as an expected outcome
            // (a 409 "changed elsewhere", a 422 "not a link I know") — the
            // caller shows them in place; the crash popup would say the same
            // thing again, dressed as a failure.
            if (!(opts.quiet || []).includes(resp.status) && !_maybeAuthModal(path, resp.status, text)) {
                _popError('POST', path, resp.status, text);
            }
            throw new Error(`API ${resp.status}: ${text}`);
        }
        return resp.json();
    },

    /* ── Core transport: PATCH / DELETE ─────────────────────────
     * Same JSON + error-handling contract as post(). Used by REST-style
     * resources such as the accounts registry (/api/accounts/{id}).
     */
    async patch(path, body = {}) {
        if (_API_DEBUG) console.log('[API] PATCH', path, body);
        const resp = await fetch(path, {
            method: 'PATCH',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(body),
        });
        if (!resp.ok) {
            const text = await resp.text();
            _popError('PATCH', path, resp.status, text);
            throw new Error(`API ${resp.status}: ${text}`);
        }
        return resp.json();
    },

    async put(path, body = {}, opts = {}) {
        if (_API_DEBUG) console.log('[API] PUT', path, body);
        const resp = await fetch(path, {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(body),
        });
        if (!resp.ok) {
            const text = await resp.text();
            if (!(opts.quiet || []).includes(resp.status)) _popError('PUT', path, resp.status, text);
            throw new Error(`API ${resp.status}: ${text}`);
        }
        return resp.json();
    },

    async del(path) {
        if (_API_DEBUG) console.log('[API] DELETE', path);
        const resp = await fetch(path, { method: 'DELETE' });
        if (!resp.ok) {
            const text = await resp.text();
            _popError('DELETE', path, resp.status, text);
            throw new Error(`API ${resp.status}: ${text}`);
        }
        return resp.json();
    },

    /* ── Accounts registry (multi-account) ─────────────────────── */
    getAccounts(platform) { return this.get('/api/accounts', platform ? { platform } : {}); },
    createAccount(body) { return this.post('/api/accounts', body); },
    updateAccount(id, body) { return this.patch(`/api/accounts/${id}`, body); },
    testAccountLogin(id) { return this.post(`/api/accounts/${id}/test-login`, {}); },
    deleteAccount(id) { return this.del(`/api/accounts/${id}`); },

    /* ── Personas (cross-platform account grouping) ────────────── */
    getPersonas() { return this.get('/api/personas'); },
    getPersona(id) { return this.get(`/api/personas/${id}`); },
    createPersona(body) { return this.post('/api/personas', body); },
    updatePersona(id, body) { return this.patch(`/api/personas/${id}`, body); },
    deletePersona(id) { return this.del(`/api/personas/${id}`); },
    assignAccountPersona(accountId, personaId) {
        return this.post(`/api/accounts/${accountId}/persona`, { persona_id: personaId });
    },

    /* ── Collections (master container per piece) ─────────────── */
    getCollections() { return this.get('/api/collections'); },
    getCollection(id) { return this.get(`/api/collections/${id}`); },
    createCollection(body) { return this.post('/api/collections', body); },
    updateCollection(id, body) { return this.patch(`/api/collections/${id}`, body); },
    deleteCollection(id) { return this.del(`/api/collections/${id}`); },
    addCollectionMember(id, body) { return this.post(`/api/collections/${id}/members`, body); },
    removeCollectionMember(id, memberType, memberRef) {
        return this.del(`/api/collections/${id}/members?member_type=${encodeURIComponent(memberType)}&member_ref=${encodeURIComponent(memberRef)}`);
    },
    // Combined cross-platform growth chart + merge suggestions (folded in from
    // the retired Cross-Platform Links screen — 2.113.0).
    getCollectionSnapshots(id) { return this.get(`/api/collections/${id}/snapshots`); },
    getCollectionSuggestions() { return this.get('/api/collections/suggestions'); },
    // Native pixel-hash scan (no AI) — hashes local artwork + allowlisted
    // thumbnails so image-based suggestions can surface (2.114.0).
    scanImageHashes(limit) { return this.post(`/api/collections/hash-scan${limit ? `?limit=${limit}` : ''}`, {}); },

    /* ── Commissions (client / commission tracker — gap-wave-5 §4) ── */
    getCommissions(archived = false) { return this.get('/api/commissions', archived ? { archived: 1 } : {}); },
    getCommission(id) { return this.get(`/api/commissions/${id}`); },
    createCommission(body) { return this.post('/api/commissions', body); },
    updateCommission(id, body) { return this.patch(`/api/commissions/${id}`, body); },
    deleteCommission(id) { return this.del(`/api/commissions/${id}`); },
    /* Attachments (2.188) — any file, ≤25 MB. */
    getCommissionFiles(id) { return this.get(`/api/commissions/${id}/files`); },
    uploadCommissionFile(id, file) {
        const fd = new FormData();
        fd.append('file', file);
        return fetch(`/api/commissions/${id}/files`, { method: 'POST', body: fd })
            .then(r => { if (!r.ok) return r.text().then(t => { throw new Error(t || `Upload failed: ${r.status}`); }); return r.json(); });
    },
    deleteCommissionFile(id, filename) {
        return this.del(`/api/commissions/${id}/files/${encodeURIComponent(filename)}`);
    },

    /* ── Masterpieces (master record per image — Phase 1/2 read, Phase 3 write) ── */
    // Artist registry (3.10.0) — 44 artists / 134 handles verified across ten
    // platforms, feeding the Masterpiece artist editor's auto-fill.
    listArtists(q = '', withCounts = false) {
        const qs = [];
        if (q) qs.push(`q=${encodeURIComponent(q)}`);
        if (withCounts) qs.push('with_counts=true');
        return this.get('/api/artists' + (qs.length ? '?' + qs.join('&') : ''));
    },
    /* Rename previews by default. The artist's name is stored INLINE on every
       masterpiece.json, so a rename rewrites artwork metadata — never done
       without showing which pieces change first. */
    renameArtist(key, newName, apply = false) {
        return this.post(`/api/artists/${encodeURIComponent(key)}/rename`,
                         { new_name: newName, apply: !!apply });
    },
    resolveArtist(name) { return this.get(`/api/artists/resolve?name=${encodeURIComponent(name)}`); },
    saveArtist(body) { return this.post('/api/artists', body); },
    /* Removing a handle needs its own call: upserts MERGE, so clearing a field
       cannot express "this handle was wrong, forget it" — the merge keeps it and
       the picker re-fills it from the registry next time. */
    deleteArtistHandle(key, platform) {
        return this.del(`/api/artists/${encodeURIComponent(key)}/handles/${encodeURIComponent(platform)}`);
    },

    getMasterpieces() { return this.get('/api/masterpieces'); },
    getMasterpiece(name) { return this.get(`/api/masterpieces/${encodeURIComponent(name)}`); },
    /* What each platform actually receives as tags, and what it loses (3.12.0). */
    getMasterpieceTagPreview(name) {
        return this.get(`/api/masterpieces/${encodeURIComponent(name)}/tag-preview`);
    },
    getMasterpieceSnapshots(name) { return this.get(`/api/masterpieces/${encodeURIComponent(name)}/snapshots`); },
    getMasterpieceSuggestions(name) { return this.get(`/api/masterpieces/${encodeURIComponent(name)}/suggestions`); },
    getMasterpieceDuplicates() { return this.get('/api/masterpieces/duplicates'); },
    /* Likely variant families grouped by TITLE (2.160.0) — the complement to the
       hash-based duplicates finder, for rough/final & SFW/NSFW of one piece. */
    getVariantSuggestions() { return this.get('/api/masterpieces/variant-suggestions'); },
    dismissVariantFamily(names) { return this.post('/api/masterpieces/not-variant', { names }); },
    matchMasterpiece(platform, submissionId) {
        return this.get('/api/masterpieces/match', { platform, submission_id: submissionId });
    },
    /* Swap a Masterpiece's canonical image (keeps metadata + site links; the old
       file stays in the folder as a gallery alternate). */
    replaceMasterpieceImage(name, file) {
        const fd = new FormData();
        fd.append('file', file);
        return fetch(`/api/masterpieces/${encodeURIComponent(name)}/image`,
            { method: 'POST', body: fd })
            .then(async r => {
                if (!r.ok) {
                    const d = await r.json().catch(() => ({}));
                    throw new Error(d.detail || `Replace failed: ${r.status}`);
                }
                return r.json();
            });
    },
    mergeMasterpieces(keep, drop) { return this.post('/api/masterpieces/merge', { keep, drop }); },
    dismissMasterpieceDuplicate(names) { return this.post('/api/masterpieces/not-duplicate', { names }); },
    // Junk status: 'junk' hides it from the grid (kept on disk, reversible); '' restores.
    setMasterpieceStatus(name, status) {
        return this.post(`/api/masterpieces/${encodeURIComponent(name)}/status`, { status });
    },
    // Variants (2.158.0): fold another Masterpiece in as a labeled variant (stats kept),
    // declare an existing folder image as a variant, demote one, attribute a member.
    mergeAsVariant(body) { return this.post('/api/masterpieces/merge-as-variant', body); },
    declareMasterpieceVariant(name, body) {
        return this.post(`/api/masterpieces/${encodeURIComponent(name)}/variants`, body);
    },
    /* Upload a fresh image straight in as a labeled variant (2.190.2). */
    uploadMasterpieceVariant(name, file, label, rating = '') {
        const fd = new FormData();
        fd.append('file', file);
        fd.append('label', label || '');
        fd.append('rating', rating || '');
        return fetch(`/api/masterpieces/${encodeURIComponent(name)}/variants/upload`,
            { method: 'POST', body: fd })
            .then(r => { if (!r.ok) return r.text().then(t => { throw new Error(t || `Upload failed: ${r.status}`); }); return r.json(); });
    },
    deleteMasterpieceVariant(name, key) {
        return this.del(`/api/masterpieces/${encodeURIComponent(name)}/variants/${encodeURIComponent(key)}`);
    },
    /* Rename/re-label a variant (2.189.0). Changing `key` migrates the members'
       variant_key, so per-variant stats survive the rename. */
    renameMasterpieceVariant(name, key, body) {
        return this.patch(`/api/masterpieces/${encodeURIComponent(name)}/variants/${encodeURIComponent(key)}`, body);
    },
    /* Separate a variant out into its own Masterpiece — the inverse of
       mergeAsVariant. Its image and members move with it (2.189.0). */
    splitMasterpieceVariant(name, key, body = {}) {
        return this.post(`/api/masterpieces/${encodeURIComponent(name)}/variants/${encodeURIComponent(key)}/split`, body);
    },
    /* Separate an UNDECLARED folder image (an "Alt N" chip from a multi-image
       import) into its own Masterpiece. Declares it as a variant and splits it
       in one server-side call (3.36.0). */
    splitMasterpieceImage(name, image, body = {}) {
        return this.post(`/api/masterpieces/${encodeURIComponent(name)}/images/split`,
            { image, ...body });
    },
    setMasterpieceMemberVariant(name, body) {
        return this.patch(`/api/masterpieces/${encodeURIComponent(name)}/members/variant`, body);
    },

    // "What's new" changelog since the version this browser last saw (update popup).
    getWhatsNew(since) { return this.get('/api/whatsnew', { since: since || '' }); },
    // Promote a discovered/imported submission into a Masterpiece (+ seed primary member).
    promoteMasterpiece(platform, submissionId) {
        return this.post('/api/masterpieces', { from: { platform, submission_id: String(submissionId) } });
    },
    addMasterpieceMember(name, body) { return this.post(`/api/masterpieces/${encodeURIComponent(name)}/members`, body); },
    // Link a site upload from a pasted URL (3.14.0). PREVIEWS by default — the
    // interesting answer is usually "that post IS known, it is just filed under
    // a different work", and linking silently would hide that. Pass
    // {confirm: true} to perform it.
    linkMasterpieceByUrl(name, body) {
        return this.post(`/api/masterpieces/${encodeURIComponent(name)}/link-url`, body);
    },
    /* Forget that this piece was posted to `platform` (3.17.0). Clears the
       publications row AND the stats member — both dim the platform on the
       detail page, so clearing one is not a fix. `keepMember` drops only the
       publication, for a post that still exists under the wrong account. */
    forgetMasterpiecePublication(name, platform, { keepMember = false } = {}) {
        const q = new URLSearchParams({
            platform, confirm_platform: platform,
            keep_member: keepMember ? 'true' : 'false',
        });
        return this.del(`/api/masterpieces/${encodeURIComponent(name)}/publication?${q}`);
    },

    removeMasterpieceMember(name, platform, submissionId) {
        return this.del(`/api/masterpieces/${encodeURIComponent(name)}/members?platform=${encodeURIComponent(platform)}&submission_id=${encodeURIComponent(submissionId)}`);
    },
    // Wrong-link auditor (2.192.0): members whose platform image doesn't match
    // the piece locally (perceptual hash). Read-only scan.
    masterpieceMislinkAudit() { return this.get('/api/masterpieces/mislink-audit'); },
    // Unfiled posts (3.16.0): publications whose work no longer exists on disk.
    getUnfiledPosts() { return this.get('/api/masterpieces/unfiled-posts'); },
    // Canonical edit (writes masterpiece.json) + Sync-all (push to editable members).
    patchMasterpiece(name, body) { return this.patch(`/api/masterpieces/${encodeURIComponent(name)}`, body); },
    syncMasterpiece(name, body = {}) { return this.post(`/api/masterpieces/${encodeURIComponent(name)}/sync`, body); },

    /* ── IB (Inkbunny) convenience methods ─────────────────────
     * General status, submission CRUD, snapshot history, aggregation,
     * comparison, polling control, session management, authentication,
     * credential/preference storage, and Telegram notification wiring.
     * These are the "default" platform endpoints (no /fa/ or /ws/ prefix).
     */
    getStatus() { return this.get('/api/status'); },
    getSummary(params) { return this.get('/api/summary', params); },
    getSubmissions(params) { return this.get('/api/submissions', params); },
    getSubmission(id) { return this.get(`/api/submissions/${id}`); },
    getSnapshots(id, params) { return this.get(`/api/submissions/${id}/snapshots`, params); },
    getAggregate(params) { return this.get('/api/aggregate', params); },
    getComparison(ids, params) { return this.get('/api/comparison', { ids: ids.join(','), ...params }); },
    getPollLog(limit) { return this.get('/api/poll_log', { limit }); },
    triggerPoll() { return this.post('/api/poll/trigger'); },
    // Poll one platform, optionally scoped to a single account. accountId null/''
    // → poll every enabled account for the platform (not just the default).
    triggerAccountPoll(code, accountId) {
        const q = (accountId !== null && accountId !== undefined && accountId !== '')
            ? `?account_id=${encodeURIComponent(accountId)}` : '';
        return this.post(`/api/poll/trigger/${code}${q}`);
    },
    fullResync() { return this.post('/api/poll/full-resync'); },
    pausePolling() { return this.post('/api/poll/pause'); },
    resumePolling() { return this.post('/api/poll/resume'); },
    getPollPaused() { return this.get('/api/poll/paused'); },
    // Per-platform pause/resume (2.103.0) — code is a platform code (fa, bsky, …).
    pausePlatformPolling(code) { return this.post(`/api/poll/pause/${code}`); },
    resumePlatformPolling(code) { return this.post(`/api/poll/resume/${code}`); },
    getLogs(params = {}) { return this.get('/api/logs', params); },
    clearSession() { return this.post('/api/session/clear'); },
    getAuthStatus() { return this.get('/api/auth/status'); },
    authLogin(data) { return this.post('/api/auth/login', data); },
    authLogout() { return this.post('/api/auth/logout'); },
    getPollProgress() { return this.get('/api/poll/progress'); },
    // 2.16.9: single-fetch combined endpoint for the global progress
    // ticker. Returns { ib: {...}, fa: {...}, ws: {...}, ... } in one
    // request instead of fanning out to 11 per-platform endpoints.
    getAllPollProgress() { return this.get('/api/poll/all-progress'); },
    // Per-platform health snapshot (single fetch for sidebar dots,
    // header subtitles, and throttle banners).
    getPlatformsHealth() { return this.get('/api/platforms/health'); },
    getPlatformSessions() { return this.get('/api/platforms/sessions'); },
    triggerSessionCheck() { return this.post('/api/platforms/sessions/check', {}); },
    getCredentialAge() { return this.get('/api/platforms/credential-age'); },
    getBackupInfo() { return this.get('/api/backup/info'); },
    importBackup(file, onProgress) {
        const fd = new FormData();
        fd.append('file', file);
        return this._upload('/api/backup/import', fd, onProgress);
    },
    // Mute/unmute a platform's session-health alert (auto-clears on recovery).
    muteSessionAlert(code, muted) { return this.post('/api/platforms/sessions/mute', { code, muted }); },
    getNotifications(limit) { return this.get('/api/notifications', limit ? { limit } : {}); },
    markNotificationsRead() { return this.post('/api/notifications/mark-read', {}); },
    clearNotifications() { return this.post('/api/notifications/clear', {}); },
    // Unified system-event feed (poll_log + posting_log merged) for
    // the Overview's "Recent System Events" panel.
    getRecentActivity(limit = 30) { return this.get('/api/activity/recent', { limit }); },
    getCredentials() { return this.get('/api/settings/credentials'); },
    saveCredentials(data) { return this.post('/api/settings/credentials', data); },
    getPreferences() { return this.get('/api/settings/preferences'); },
    savePreferences(data) { return this.post('/api/settings/preferences', data); },
    getTelegram() { return this.get('/api/settings/telegram'); },
    connectTelegram(data) { return this.post('/api/settings/telegram', data); },
    testTelegram() { return this.post('/api/settings/telegram/test'); },
    disconnectTelegram() { return this.post('/api/settings/telegram/disconnect'); },
    getTelegramFeatures() { return this.get('/api/settings/telegram/features'); },
    setTelegramFeatures(data) { return this.post('/api/settings/telegram/features', data); },
    /* Telegram channel posting (Posts-module broadcast target). */
    getTelegramChannel() { return this.get('/api/settings/telegram/channel'); },
    saveTelegramChannel(data) { return this.post('/api/settings/telegram/channel', data); },
    testTelegramChannel(data) { return this.post('/api/settings/telegram/channel/test', data || {}); },
    detectTelegramChannels() { return this.post('/api/settings/telegram/channel/detect', {}); },
    /* Weekly email digest (deterministic, templated — no AI). */
    getDigestStatus() { return this.get('/api/digest/status'); },
    saveDigestSettings(data) { return this.post('/api/digest/settings', data); },
    testDigest() { return this.post('/api/digest/test'); },
    /* ── FA (FurAffinity) convenience methods ──────────────────
     * Mirror of the IB methods above, namespaced under /api/fa/.
     * Covers auth connection, status, submissions, snapshots,
     * aggregation, comparison, poll control, and resync.
     */
    getFAAuthStatus() { return this.get('/api/fa/auth/status'); },
    faConnect(data) { return this.post('/api/fa/auth/connect', data); },
    faDisconnect() { return this.post('/api/fa/auth/disconnect'); },
    getFAStatus() { return this.get('/api/fa/status'); },
    getFASummary(params) { return this.get('/api/fa/summary', params); },
    getFASubmissions(params) { return this.get('/api/fa/submissions', params); },
    getFASubmission(id) { return this.get(`/api/fa/submissions/${id}`); },
    getFASnapshots(id, params) { return this.get(`/api/fa/submissions/${id}/snapshots`, params); },
    getFAAggregate(params) { return this.get('/api/fa/aggregate', params); },
    getFAComparison(ids, params) { return this.get('/api/fa/comparison', { ids: ids.join(','), ...params }); },
    getFAPollLog(limit) { return this.get('/api/fa/poll_log', { limit }); },
    triggerFAPoll() { return this.post('/api/fa/poll/trigger'); },
    fullFAResync() { return this.post('/api/fa/poll/full-resync'); },
    getFAPollProgress() { return this.get('/api/fa/poll/progress'); },
    getWatchers() { return this.get('/api/watchers'); },
    getFAWatchers() { return this.get('/api/fa/watchers'); },
    /* ── WS (Weasyl) convenience methods ───────────────────────
     * Mirror of the IB/FA methods, namespaced under /api/ws/.
     * Covers auth connection, status, submissions, snapshots,
     * aggregation, comparison, poll control, and resync.
     */
    getWSAuthStatus() { return this.get('/api/ws/auth/status'); },
    wsConnect(data) { return this.post('/api/ws/auth/connect', data); },
    wsDisconnect() { return this.post('/api/ws/auth/disconnect'); },
    getWSStatus() { return this.get('/api/ws/status'); },
    getWSSummary(params) { return this.get('/api/ws/summary', params); },
    getWSSubmissions(params) { return this.get('/api/ws/submissions', params); },
    getWSSubmission(id) { return this.get(`/api/ws/submissions/${id}`); },
    getWSSnapshots(id, params) { return this.get(`/api/ws/submissions/${id}/snapshots`, params); },
    getWSAggregate(params) { return this.get('/api/ws/aggregate', params); },
    getWSComparison(ids, params) { return this.get('/api/ws/comparison', { ids: ids.join(','), ...params }); },
    getWSPollLog(limit) { return this.get('/api/ws/poll_log', { limit }); },
    triggerWSPoll() { return this.post('/api/ws/poll/trigger'); },
    fullWSResync() { return this.post('/api/ws/poll/full-resync'); },
    getWSPollProgress() { return this.get('/api/ws/poll/progress'); },
    /* ── SF (SoFurry) convenience methods ────────────────────────
     * Mirror of the WS methods, namespaced under /api/sf/.
     * SoFurry uses email/password auth instead of API key.
     */
    getSFAuthStatus() { return this.get('/api/sf/auth/status'); },
    sfConnect(data) { return this.post('/api/sf/auth/connect', data); },
    sfDisconnect() { return this.post('/api/sf/auth/disconnect'); },
    getSFStatus() { return this.get('/api/sf/status'); },
    getSFSummary(params) { return this.get('/api/sf/summary', params); },
    getSFSubmissions(params) { return this.get('/api/sf/submissions', params); },
    getSFSubmission(id) { return this.get(`/api/sf/submissions/${id}`); },
    getSFSnapshots(id, params) { return this.get(`/api/sf/submissions/${id}/snapshots`, params); },
    getSFAggregate(params) { return this.get('/api/sf/aggregate', params); },
    getSFComparison(ids, params) { return this.get('/api/sf/comparison', { ids: ids.join(','), ...params }); },
    getSFPollLog(limit) { return this.get('/api/sf/poll_log', { limit }); },
    triggerSFPoll() { return this.post('/api/sf/poll/trigger'); },
    fullSFResync() { return this.post('/api/sf/poll/full-resync'); },
    getSFPollProgress() { return this.get('/api/sf/poll/progress'); },
    getSFWatchers() { return this.get('/api/sf/watchers'); },
    /* ── SQW (SquidgeWorld) convenience methods ──────────────────
     * Mirror of the SF methods, namespaced under /api/sqw/.
     * SquidgeWorld uses username/password auth to track a target user.
     */
    getSQWAuthStatus() { return this.get('/api/sqw/auth/status'); },
    sqwConnect(data) { return this.post('/api/sqw/auth/connect', data); },
    sqwDisconnect() { return this.post('/api/sqw/auth/disconnect'); },
    getSQWStatus() { return this.get('/api/sqw/status'); },
    getSQWSummary(params) { return this.get('/api/sqw/summary', params); },
    getSQWSubmissions(params) { return this.get('/api/sqw/submissions', params); },
    getSQWSubmission(id) { return this.get(`/api/sqw/submissions/${id}`); },
    getSQWSnapshots(id, params) { return this.get(`/api/sqw/submissions/${id}/snapshots`, params); },
    getSQWAggregate(params) { return this.get('/api/sqw/aggregate', params); },
    getSQWComparison(ids, params) { return this.get('/api/sqw/comparison', { ids: ids.join(','), ...params }); },
    getSQWPollLog(limit) { return this.get('/api/sqw/poll_log', { limit }); },
    triggerSQWPoll() { return this.post('/api/sqw/poll/trigger'); },
    fullSQWResync() { return this.post('/api/sqw/poll/full-resync'); },
    getSQWPollProgress() { return this.get('/api/sqw/poll/progress'); },
    /* ── AO3 (Archive of Our Own) convenience methods ──────────────
     * Mirror of the SQW methods, namespaced under /api/ao3/.
     * AO3 uses username/password auth to track a target user.
     */
    getAO3AuthStatus() { return this.get('/api/ao3/auth/status'); },
    ao3Connect(data) { return this.post('/api/ao3/auth/connect', data); },
    ao3Disconnect() { return this.post('/api/ao3/auth/disconnect'); },
    getAO3Status() { return this.get('/api/ao3/status'); },
    getAO3Summary(params) { return this.get('/api/ao3/summary', params); },
    getAO3Submissions(params) { return this.get('/api/ao3/submissions', params); },
    getAO3Submission(id) { return this.get(`/api/ao3/submissions/${id}`); },
    getAO3Snapshots(id, params) { return this.get(`/api/ao3/submissions/${id}/snapshots`, params); },
    getAO3Aggregate(params) { return this.get('/api/ao3/aggregate', params); },
    getAO3Comparison(ids, params) { return this.get('/api/ao3/comparison', { ids: ids.join(','), ...params }); },
    getAO3PollLog(limit) { return this.get('/api/ao3/poll_log', { limit }); },
    triggerAO3Poll() { return this.post('/api/ao3/poll/trigger'); },
    fullAO3Resync() { return this.post('/api/ao3/poll/full-resync'); },
    getAO3PollProgress() { return this.get('/api/ao3/poll/progress'); },
    /* ── DA (DeviantArt) convenience methods ────────────────────────
     * Namespaced under /api/da/. Polling uses the official OAuth2 API —
     * daConnect sends { client_id, client_secret, target_user }.
     */
    getDAAuthStatus() { return this.get('/api/da/auth/status'); },
    daConnect(data) { return this.post('/api/da/auth/connect', data); },
    daDisconnect() { return this.post('/api/da/auth/disconnect'); },
    /* Posting is a SECOND authorisation on the same app: polling mints an
     * app-only client-credentials token, posting acts as the user and so needs
     * a browser approval that only DA can grant. These two cover that half. */
    getDAPostingStatus(accountId) {
        return this.get('/api/da/auth/posting-status',
                        accountId ? { account_id: accountId } : undefined);
    },
    getDAAuthorizeUrl(accountId) {
        return this.get('/api/da/auth/authorize-url',
                        accountId ? { account_id: accountId } : undefined);
    },
    getDAStatus() { return this.get('/api/da/status'); },
    getDASummary(params) { return this.get('/api/da/summary', params); },
    getFollowers(platform, params) { return this.get('/api/followers/' + platform, params); },
    getDASubmissions(params) { return this.get('/api/da/submissions', params); },
    getDASubmission(id) { return this.get(`/api/da/submissions/${id}`); },
    getDASnapshots(id, params) { return this.get(`/api/da/submissions/${id}/snapshots`, params); },
    getDAAggregate(params) { return this.get('/api/da/aggregate', params); },
    getDAComparison(ids, params) { return this.get('/api/da/comparison', { ids: ids.join(','), ...params }); },
    getDAPollLog(limit) { return this.get('/api/da/poll_log', { limit }); },
    triggerDAPoll() { return this.post('/api/da/poll/trigger'); },
    fullDAResync() { return this.post('/api/da/poll/full-resync'); },
    getDAPollProgress() { return this.get('/api/da/poll/progress'); },
    /* ── WP (Wattpad) convenience methods ─────────────────────────
     * Mirror of the DA methods, namespaced under /api/wp/.
     * Wattpad uses username-only auth (no password or cookie needed).
     */
    getWPAuthStatus() { return this.get('/api/wp/auth/status'); },
    wpConnect(data) { return this.post('/api/wp/auth/connect', data); },
    wpDisconnect() { return this.post('/api/wp/auth/disconnect'); },
    getWPStatus() { return this.get('/api/wp/status'); },
    getWPSummary(params) { return this.get('/api/wp/summary', params); },
    getWPSubmissions(params) { return this.get('/api/wp/submissions', params); },
    getWPSubmission(id) { return this.get(`/api/wp/submissions/${id}`); },
    getWPSnapshots(id, params) { return this.get(`/api/wp/submissions/${id}/snapshots`, params); },
    getWPAggregate(params) { return this.get('/api/wp/aggregate', params); },
    getWPComparison(ids, params) { return this.get('/api/wp/comparison', { ids: ids.join(','), ...params }); },
    getWPPollLog(limit) { return this.get('/api/wp/poll_log', { limit }); },
    triggerWPPoll() { return this.post('/api/wp/poll/trigger'); },
    fullWPResync() { return this.post('/api/wp/poll/full-resync'); },
    getWPPollProgress() { return this.get('/api/wp/poll/progress'); },
    /* ── IK (Itaku) convenience methods ──────────────────────────
     * Mirror of the WP methods, namespaced under /api/ik/.
     * Itaku uses username-only auth (no password or cookie needed).
     * Tracks likes, comments, reshares (no views).
     */
    getIKAuthStatus() { return this.get('/api/ik/auth/status'); },
    ikConnect(data) { return this.post('/api/ik/auth/connect', data); },
    ikSetToken(auth_token) { return this.post('/api/ik/auth/token', { auth_token }); },
    ikDisconnect() { return this.post('/api/ik/auth/disconnect'); },
    getIKStatus() { return this.get('/api/ik/status'); },
    getIKSummary(params) { return this.get('/api/ik/summary', params); },
    getIKSubmissions(params) { return this.get('/api/ik/submissions', params); },
    getIKSubmission(id) { return this.get(`/api/ik/submissions/${id}`); },
    getIKSnapshots(id, params) { return this.get(`/api/ik/submissions/${id}/snapshots`, params); },
    getIKAggregate(params) { return this.get('/api/ik/aggregate', params); },
    getIKComparison(ids, params) { return this.get('/api/ik/comparison', { ids: ids.join(','), ...params }); },
    getIKPollLog(limit) { return this.get('/api/ik/poll_log', { limit }); },
    triggerIKPoll() { return this.post('/api/ik/poll/trigger'); },
    fullIKResync() { return this.post('/api/ik/poll/full-resync'); },
    getIKPollProgress() { return this.get('/api/ik/poll/progress'); },
    /* ── BSKY (Bluesky) convenience methods ───────────────────────
     * AT Protocol API with app password auth. Posts identified by AT URIs.
     * Tracks likes, reposts, replies, quotes (no views).
     */
    getBSKYAuthStatus() { return this.get('/api/bsky/auth/status'); },
    bskyConnect(data) { return this.post('/api/bsky/auth/connect', data); },
    bskyDisconnect() { return this.post('/api/bsky/auth/disconnect'); },
    getBSKYStatus() { return this.get('/api/bsky/status'); },
    getBSKYSummary(params) { return this.get('/api/bsky/summary', params); },
    getBSKYSubmissions(params) { return this.get('/api/bsky/submissions', params); },
    getBSKYSubmission(id) { return this.get(`/api/bsky/submissions/${encodeURIComponent(id)}`); },
    getBSKYSnapshots(id, params) { return this.get(`/api/bsky/submissions/${encodeURIComponent(id)}/snapshots`, params); },
    getBSKYAggregate(params) { return this.get('/api/bsky/aggregate', params); },
    getBSKYComparison(ids, params) { return this.get('/api/bsky/comparison', { ids: ids.join(','), ...params }); },
    getBSKYPollLog(limit) { return this.get('/api/bsky/poll_log', { limit }); },
    triggerBSKYPoll() { return this.post('/api/bsky/poll/trigger'); },
    fullBSKYResync() { return this.post('/api/bsky/poll/full-resync'); },
    getBSKYPollProgress() { return this.get('/api/bsky/poll/progress'); },
    /* ── MAST (Mastodon) convenience methods ──────────────────────
     * Per-instance REST API. Posts identified by ActivityPub URIs.
     * Tracks likes (favourites), reposts (boosts), replies.
     */
    getMASTAuthStatus() { return this.get('/api/mast/auth/status'); },
    mastConnect(data) { return this.post('/api/mast/auth/connect', data); },
    mastDisconnect() { return this.post('/api/mast/auth/disconnect'); },
    getMASTStatus() { return this.get('/api/mast/status'); },
    getMASTSummary(params) { return this.get('/api/mast/summary', params); },
    getMASTSubmissions(params) { return this.get('/api/mast/submissions', params); },
    getMASTSubmission(id) { return this.get(`/api/mast/submissions/${encodeURIComponent(id)}`); },
    getMASTSnapshots(id, params) { return this.get(`/api/mast/submissions/${encodeURIComponent(id)}/snapshots`, params); },
    getMASTAggregate(params) { return this.get('/api/mast/aggregate', params); },
    getMASTComparison(ids, params) { return this.get('/api/mast/comparison', { ids: ids.join(','), ...params }); },
    getMASTPollLog(limit) { return this.get('/api/mast/poll_log', { limit }); },
    triggerMASTPoll() { return this.post('/api/mast/poll/trigger'); },
    fullMASTResync() { return this.post('/api/mast/poll/full-resync'); },
    getMASTPollProgress() { return this.get('/api/mast/poll/progress'); },
    /* ── TUM (Tumblr) convenience methods ─────────────────────────
     * Read-only v2 API (API key + blog). Posts identified by numeric ids.
     * Single engagement metric: notes (likes + reblogs + replies).
     */
    getTUMAuthStatus() { return this.get('/api/tum/auth/status'); },
    tumConnect(data) { return this.post('/api/tum/auth/connect', data); },
    tumDisconnect() { return this.post('/api/tum/auth/disconnect'); },
    getTUMStatus() { return this.get('/api/tum/status'); },
    getTUMSummary(params) { return this.get('/api/tum/summary', params); },
    getTUMSubmissions(params) { return this.get('/api/tum/submissions', params); },
    getTUMSubmission(id) { return this.get(`/api/tum/submissions/${encodeURIComponent(id)}`); },
    getTUMSnapshots(id, params) { return this.get(`/api/tum/submissions/${encodeURIComponent(id)}/snapshots`, params); },
    getTUMAggregate(params) { return this.get('/api/tum/aggregate', params); },
    getTUMComparison(ids, params) { return this.get('/api/tum/comparison', { ids: ids.join(','), ...params }); },
    getTUMPollLog(limit) { return this.get('/api/tum/poll_log', { limit }); },
    triggerTUMPoll() { return this.post('/api/tum/poll/trigger'); },
    fullTUMResync() { return this.post('/api/tum/poll/full-resync'); },
    getTUMPollProgress() { return this.get('/api/tum/poll/progress'); },
    /* ── PIX (Pixiv) convenience methods ──────────────────────────
     * App-API (OAuth refresh token). Works identified by namespaced ids.
     * Gallery metrics: views, favorites_count (bookmarks), comments_count.
     */
    getPIXAuthStatus() { return this.get('/api/pix/auth/status'); },
    pixConnect(data) { return this.post('/api/pix/auth/connect', data); },
    pixDisconnect() { return this.post('/api/pix/auth/disconnect'); },
    getPIXStatus() { return this.get('/api/pix/status'); },
    getPIXSummary(params) { return this.get('/api/pix/summary', params); },
    getPIXSubmissions(params) { return this.get('/api/pix/submissions', params); },
    getPIXSubmission(id) { return this.get(`/api/pix/submissions/${encodeURIComponent(id)}`); },
    getPIXSnapshots(id, params) { return this.get(`/api/pix/submissions/${encodeURIComponent(id)}/snapshots`, params); },
    getPIXAggregate(params) { return this.get('/api/pix/aggregate', params); },
    getPIXComparison(ids, params) { return this.get('/api/pix/comparison', { ids: ids.join(','), ...params }); },
    getPIXPollLog(limit) { return this.get('/api/pix/poll_log', { limit }); },
    triggerPIXPoll() { return this.post('/api/pix/poll/trigger'); },
    fullPIXResync() { return this.post('/api/pix/poll/full-resync'); },
    getPIXPollProgress() { return this.get('/api/pix/poll/progress'); },
    /* ── E621 convenience methods ─────────────────────────────────
     * Official REST API (HTTP Basic: username + API key). Poll-only.
     * Metrics: score (can be negative), favorites_count, comments_count.
     */
    getE621AuthStatus() { return this.get('/api/e621/auth/status'); },
    e621Connect(data) { return this.post('/api/e621/auth/connect', data); },
    e621Disconnect() { return this.post('/api/e621/auth/disconnect'); },
    getE621Status() { return this.get('/api/e621/status'); },
    getE621Summary(params) { return this.get('/api/e621/summary', params); },
    getE621Submissions(params) { return this.get('/api/e621/submissions', params); },
    getE621Submission(id) { return this.get(`/api/e621/submissions/${encodeURIComponent(id)}`); },
    getE621Snapshots(id, params) { return this.get(`/api/e621/submissions/${encodeURIComponent(id)}/snapshots`, params); },
    getE621Aggregate(params) { return this.get('/api/e621/aggregate', params); },
    getE621Comparison(ids, params) { return this.get('/api/e621/comparison', { ids: ids.join(','), ...params }); },
    getE621PollLog(limit) { return this.get('/api/e621/poll_log', { limit }); },
    triggerE621Poll() { return this.post('/api/e621/poll/trigger'); },
    fullE621Resync() { return this.post('/api/e621/poll/full-resync'); },
    getE621PollProgress() { return this.get('/api/e621/poll/progress'); },
    /* ── FurryNetwork convenience methods ─────────────────────────── */
    getFNAuthStatus() { return this.get('/api/fn/auth/status'); },
    fnConnect(data) { return this.post('/api/fn/auth/connect', data); },
    fnDisconnect() { return this.post('/api/fn/auth/disconnect'); },
    getFNStatus() { return this.get('/api/fn/status'); },
    getFNSummary(params) { return this.get('/api/fn/summary', params); },
    getFNSubmissions(params) { return this.get('/api/fn/submissions', params); },
    getFNSubmission(id) { return this.get(`/api/fn/submissions/${encodeURIComponent(id)}`); },
    getFNSnapshots(id, params) { return this.get(`/api/fn/submissions/${encodeURIComponent(id)}/snapshots`, params); },
    getFNAggregate(params) { return this.get('/api/fn/aggregate', params); },
    getFNComparison(ids, params) { return this.get('/api/fn/comparison', { ids: ids.join(','), ...params }); },
    getFNPollLog(limit) { return this.get('/api/fn/poll_log', { limit }); },
    triggerFNPoll() { return this.post('/api/fn/poll/trigger'); },
    fullFNResync() { return this.post('/api/fn/poll/full-resync'); },
    getFNPollProgress() { return this.get('/api/fn/poll/progress'); },
    /* ── Furbooru convenience methods ─────────────────────────────
     * Philomena public read API (booru; SCORE model like e621).
     * username required; API key optional (raises the anon rate cap).
     */
    getFBRAuthStatus() { return this.get('/api/fbr/auth/status'); },
    fbrConnect(data) { return this.post('/api/fbr/auth/connect', data); },
    fbrDisconnect() { return this.post('/api/fbr/auth/disconnect'); },
    getFBRStatus() { return this.get('/api/fbr/status'); },
    getFBRSummary(params) { return this.get('/api/fbr/summary', params); },
    getFBRSubmissions(params) { return this.get('/api/fbr/submissions', params); },
    getFBRSubmission(id) { return this.get(`/api/fbr/submissions/${encodeURIComponent(id)}`); },
    getFBRSnapshots(id, params) { return this.get(`/api/fbr/submissions/${encodeURIComponent(id)}/snapshots`, params); },
    getFBRAggregate(params) { return this.get('/api/fbr/aggregate', params); },
    getFBRComparison(ids, params) { return this.get('/api/fbr/comparison', { ids: ids.join(','), ...params }); },
    getFBRPollLog(limit) { return this.get('/api/fbr/poll_log', { limit }); },
    triggerFBRPoll() { return this.post('/api/fbr/poll/trigger'); },
    fullFBRResync() { return this.post('/api/fbr/poll/full-resync'); },
    getFBRPollProgress() { return this.get('/api/fbr/poll/progress'); },
    /* ── Telegram convenience methods ─────────────────────────────
     * Channel analytics. There are deliberately no auth methods here — the
     * bot token and channel are set through /api/settings/telegram/*, which
     * predates this router and is what the first-run wizard drives.
     * ONE metric: reactions. No views (not in the Bot API at all) and no
     * comments (channel discussion lives in a separate linked group).
     * Submission ids are "<chat_id>:<message_id>", so every path-embedded id
     * MUST be encodeURIComponent'd — a raw ':' is legal in a path segment but
     * a private channel's id also begins with '-', and the CSV writer quotes
     * it for the same reason.
     */
    getTGStatus() { return this.get('/api/tg/status'); },
    getTGSummary(params) { return this.get('/api/tg/summary', params); },
    getTGSubmissions(params) { return this.get('/api/tg/submissions', params); },
    getTGSubmission(id) { return this.get(`/api/tg/submissions/${encodeURIComponent(id)}`); },
    getTGSnapshots(id, params) { return this.get(`/api/tg/submissions/${encodeURIComponent(id)}/snapshots`, params); },
    getTGAggregate(params) { return this.get('/api/tg/aggregate', params); },
    getTGComparison(ids, params) { return this.get('/api/tg/comparison', { ids: ids.join(','), ...params }); },
    getTGPollLog(limit) { return this.get('/api/tg/poll_log', { limit }); },
    triggerTGPoll() { return this.post('/api/tg/poll/trigger'); },
    fullTGResync() { return this.post('/api/tg/poll/full-resync'); },
    getTGPollProgress() { return this.get('/api/tg/poll/progress'); },
    /* ── THR (Threads) convenience methods ────────────────────────
     * Official Graph API (OAuth long-lived token). Posts identified by media ids.
     * Metrics: views, likes, reposts, replies, quotes.
     */
    getTHRAuthStatus() { return this.get('/api/thr/auth/status'); },
    thrConnect(data) { return this.post('/api/thr/auth/connect', data); },
    thrDisconnect() { return this.post('/api/thr/auth/disconnect'); },
    getTHRStatus() { return this.get('/api/thr/status'); },
    getTHRSummary(params) { return this.get('/api/thr/summary', params); },
    getTHRSubmissions(params) { return this.get('/api/thr/submissions', params); },
    getTHRSubmission(id) { return this.get(`/api/thr/submissions/${encodeURIComponent(id)}`); },
    getTHRSnapshots(id, params) { return this.get(`/api/thr/submissions/${encodeURIComponent(id)}/snapshots`, params); },
    getTHRAggregate(params) { return this.get('/api/thr/aggregate', params); },
    getTHRComparison(ids, params) { return this.get('/api/thr/comparison', { ids: ids.join(','), ...params }); },
    getTHRPollLog(limit) { return this.get('/api/thr/poll_log', { limit }); },
    triggerTHRPoll() { return this.post('/api/thr/poll/trigger'); },
    fullTHRResync() { return this.post('/api/thr/poll/full-resync'); },
    getTHRPollProgress() { return this.get('/api/thr/poll/progress'); },
    /* ── IG (Instagram) convenience methods ───────────────────────
     * Official Graph API (OAuth long-lived token). Posts identified by media ids.
     * Metrics: views, reach, likes, comments, saved, shares.
     */
    getIGAuthStatus() { return this.get('/api/ig/auth/status'); },
    igConnect(data) { return this.post('/api/ig/auth/connect', data); },
    igDisconnect() { return this.post('/api/ig/auth/disconnect'); },
    getIGStatus() { return this.get('/api/ig/status'); },
    getIGSummary(params) { return this.get('/api/ig/summary', params); },
    getIGSubmissions(params) { return this.get('/api/ig/submissions', params); },
    getIGSubmission(id) { return this.get(`/api/ig/submissions/${encodeURIComponent(id)}`); },
    getIGSnapshots(id, params) { return this.get(`/api/ig/submissions/${encodeURIComponent(id)}/snapshots`, params); },
    getIGAggregate(params) { return this.get('/api/ig/aggregate', params); },
    getIGComparison(ids, params) { return this.get('/api/ig/comparison', { ids: ids.join(','), ...params }); },
    getIGPollLog(limit) { return this.get('/api/ig/poll_log', { limit }); },
    triggerIGPoll() { return this.post('/api/ig/poll/trigger'); },
    fullIGResync() { return this.post('/api/ig/poll/full-resync'); },
    getIGPollProgress() { return this.get('/api/ig/poll/progress'); },
    /* ── TW (X/Twitter) convenience methods ───────────────────────
     * Cookie-based GraphQL API. Tweets identified by numeric ID strings.
     * Tracks views, likes, retweets, replies, quotes, bookmarks.
     */
    getTWAuthStatus() { return this.get('/api/tw/auth/status'); },
    twConnect(data) { return this.post('/api/tw/auth/connect', data); },
    twDisconnect() { return this.post('/api/tw/auth/disconnect'); },
    twApiTokenConnect(data) { return this.post('/api/tw/api-token/connect', data); },
    twApiTokenDisconnect() { return this.post('/api/tw/api-token/disconnect'); },
    getTWStatus() { return this.get('/api/tw/status'); },
    getTWSummary(params) { return this.get('/api/tw/summary', params); },
    getTWSubmissions(params) { return this.get('/api/tw/submissions', params); },
    getTWSubmission(id) { return this.get(`/api/tw/submissions/${id}`); },
    getTWSnapshots(id, params) { return this.get(`/api/tw/submissions/${id}/snapshots`, params); },
    getTWAggregate(params) { return this.get('/api/tw/aggregate', params); },
    getTWComparison(ids, params) { return this.get('/api/tw/comparison', { ids: ids.join(','), ...params }); },
    getTWPollLog(limit) { return this.get('/api/tw/poll_log', { limit }); },
    triggerTWPoll() { return this.post('/api/tw/poll/trigger'); },
    fullTWResync() { return this.post('/api/tw/poll/full-resync'); },
    getTWPollProgress() { return this.get('/api/tw/poll/progress'); },
    /* ── Export methods ───────────────────────────────────────────
     * These trigger browser-native file downloads by opening the
     * streaming CSV endpoint in a new tab via window.open().
     * They do NOT use get()/post() because the response is a file
     * download, not JSON to be parsed in-page.
     */
    // Inkbunny's routes are unprefixed — the app began as an Inkbunny
    // analytics tool and they kept their original paths.
    _exportBase(platform) {
        return platform === 'ib' ? '/api' : `/api/${platform}`;
    },
    exportSubmissions(platform) {
        // Derived, not a literal map. The old map was hand-maintained and had
        // silently fallen behind: Instagram and Telegram were both missing, and
        // because the lookup fell back to Inkbunny's URL, asking for either
        // one downloaded INKBUNNY's CSV — wrong data under the right filename,
        // with no error to notice.
        window.open(`${this._exportBase(platform)}/export/submissions`, '_blank');
    },
    exportSnapshots(platform, id) {
        const url = `${this._exportBase(platform)}/export/snapshots`
            + (id ? `?id=${encodeURIComponent(id)}` : '');
        window.open(url, '_blank');
    },
    /* ── Groups methods ──────────────────────────────────────────
     * CRUD for user-defined submission groups.  deleteGroup and
     * removeGroupMember use raw fetch() with method: 'DELETE' because
     * the post() wrapper only supports POST — there is no delete()
     * core transport method.
     */
    getGroups() { return this.get('/api/groups'); },
    createGroup(data) { return this.post('/api/groups', data); },
    updateGroup(id, data) { return this.post(`/api/groups/${id}`, data); },
    deleteGroup(id) {
        return fetch(`/api/groups/${id}`, { method: 'DELETE' }).then(r => {
            if (!r.ok) throw new Error(`Delete failed: ${r.status}`);
            return r.json();
        });
    },
    addGroupMember(groupId, data) { return this.post(`/api/groups/${groupId}/members`, data); },
    removeGroupMember(groupId, platform, subId) {
        return fetch(`/api/groups/${groupId}/members?platform=${platform}&submission_id=${subId}`, { method: 'DELETE' }).then(r => {
            if (!r.ok) throw new Error(`Remove member failed: ${r.status}`);
            return r.json();
        });
    },
    getGroupStats(id) { return this.get(`/api/groups/${id}/stats`); },
    /* ── Analytics methods ──────────────────────────────────────
     * Aggregated cross-submission analytics: top commenters/faves
     * and trending submission discovery.
     */
    getTopFans(limit) { return this.get('/api/analytics/top-fans', { limit }); },
    getTrending(params) { return this.get('/api/analytics/trending', params); },
    /* ── Cross-Platform Links methods ────────────────────────────
     * Manages links that tie the same submission across IB/FA/WS so
     * stats can be viewed together.  deleteLink uses raw fetch() with
     * method: 'DELETE' for the same reason as deleteGroup above.
     */
    getLinks() { return this.get('/api/links'); },
    createLink(data) { return this.post('/api/links', data); },
    deleteLink(id) {
        return fetch(`/api/links/${id}`, { method: 'DELETE' }).then(r => {
            if (!r.ok) throw new Error(`Delete link failed: ${r.status}`);
            return r.json();
        });
    },
    getLinkStats(id) { return this.get(`/api/links/${id}/stats`); },
    getLinkSnapshots(id) { return this.get(`/api/links/${id}/snapshots`); },
    getLinkSuggestions() { return this.get('/api/links/suggestions'); },
    /* ── Auto-Update methods ─────────────────────────────────────
     * Checks for new PawPoller releases and applies updates
     * through the backend's self-update mechanism.
     */
    checkUpdate() { return this.get('/api/update/check'); },
    applyUpdate(data) { return this.post('/api/update/apply', data); },
    /* ── Pins methods ─────────────────────────────────────────────
     * Pin/unpin favourite submissions to dashboard tops.
     */
    getPins() { return this.get('/api/pins'); },
    addPin(data) { return this.post('/api/pins', data); },
    removePin(platform, submissionId) {
        return fetch(`/api/pins?platform=${platform}&submission_id=${submissionId}`, { method: 'DELETE' }).then(r => {
            if (!r.ok) throw new Error(`Unpin failed: ${r.status}`);
            return r.json();
        });
    },
    /* ── Goals methods ────────────────────────────────────────────
     * Track progress toward user-defined metric targets.
     */
    getGoals() { return this.get('/api/goals'); },
    createGoal(data) { return this.post('/api/goals', data); },
    deleteGoal(id) {
        return fetch(`/api/goals/${id}`, { method: 'DELETE' }).then(r => {
            if (!r.ok) throw new Error(`Delete goal failed: ${r.status}`);
            return r.json();
        });
    },
    /* ── Tags methods ─────────────────────────────────────────────
     * User-defined submission categorisation labels.
     */
    getTags() { return this.get('/api/tags'); },
    createTag(data) { return this.post('/api/tags', data); },
    deleteTag(id) {
        return fetch(`/api/tags/${id}`, { method: 'DELETE' }).then(r => {
            if (!r.ok) throw new Error(`Delete tag failed: ${r.status}`);
            return r.json();
        });
    },
    addTagToSubmission(tagId, data) { return this.post(`/api/tags/${tagId}/submissions`, data); },
    removeTagFromSubmission(tagId, platform, submissionId) {
        return fetch(`/api/tags/${tagId}/submissions?platform=${platform}&submission_id=${submissionId}`, { method: 'DELETE' }).then(r => {
            if (!r.ok) throw new Error(`Remove tag failed: ${r.status}`);
            return r.json();
        });
    },
    getTagStats(id) { return this.get(`/api/tags/${id}/stats`); },
    /* ── Backup methods ───────────────────────────────────────────
     * Database backup and restore.
     */
    downloadBackup() { window.open('/api/backup/database', '_blank'); },
    restoreBackup(formData) {
        return fetch('/api/backup/restore', { method: 'POST', body: formData }).then(r => {
            if (!r.ok) throw new Error(`Restore failed: ${r.status}`);
            return r.json();
        });
    },
    /* ── Historical Analytics ─────────────────────────────────────
     * Best periods, fastest growing, weekly growth reports.
     */
    getHistoricalAnalytics(params) { return this.get('/api/analytics/historical', params); },
    /* Repost Radar: older, well-performing artwork worth resurfacing (deterministic,
     * ranked by pooled engagement + age; no model). params: {min_age_days, limit}. */
    getRepostRadar(params) { return this.get('/api/analytics/repost-radar', params); },
    /* Tag performance: which tags/keywords earn engagement, normalised per platform
     * (deterministic, no model). params: {min_works, limit, platform}. */
    getTagPerformance(params) { return this.get('/api/analytics/tag-performance', params); },
    /* ── Dashboard Auth methods ─────────────────────────────────
     * Self-hosted session auth: login, logout, setup, password change,
     * TOTP 2FA, API keys, and Cloudflare Turnstile config.
     * Separate from Inkbunny platform auth (authLogin/authLogout above).
     */
    getDashboardStatus() { return this.get('/api/auth/dashboard-status'); },
    dashboardLogin(data) { return this.post('/api/auth/dashboard-login', data); },
    dashboardSetup(data) { return this.post('/api/auth/dashboard-setup', data); },
    dashboardLogout() { return this.post('/api/auth/dashboard-logout'); },
    dashboardChangePassword(data) { return this.post('/api/auth/dashboard-change-password', data); },
    totpSetup() { return this.post('/api/auth/totp-setup'); },
    totpEnable(data) { return this.post('/api/auth/totp-enable', data); },
    totpDisable(data) { return this.post('/api/auth/totp-disable', data); },
    /* ── Mirror / Sync (3.18.0) ───────────────────────────────────────────
       Before this the only way to sync was a CLI script run from a source
       checkout with PAWPOLLER_APPDATA_DIR set. These are the same operations
       the script performs, through the routes the app already had. */
    getMirrorStatus() { return this.get('/api/mirror/status'); },
    getMirrorDrift() { return this.get('/api/mirror/drift'); },
    startMirrorPull(body) { return this.post('/api/mirror/pull', body || {}); },
    getMirrorPullStatus() { return this.get('/api/mirror/pull/status'); },
    mirrorRestart() { return this.post('/api/mirror/restart', {}); },
    setMirrorAutoCheck(enabled) { return this.post('/api/mirror/auto-check', { enabled }); },

    getApiKeys() { return this.get('/api/auth/api-keys'); },
    createApiKey(data) { return this.post('/api/auth/api-keys', data); },
    revokeApiKey(prefix) {
        return fetch(`/api/auth/api-keys/${prefix}`, { method: 'DELETE' }).then(r => {
            if (!r.ok) throw new Error(`Revoke failed: ${r.status}`);
            return r.json();
        });
    },
    saveTurnstileConfig(data) { return this.post('/api/auth/turnstile-config', data); },

    /* ── Posting Module ───────────────────────────────────────── */
    getPostingStories() { return this.get('/api/posting/stories'); },
    getPostingStory(name) { return this.get(`/api/posting/stories/${encodeURIComponent(name)}`); },
    /* Story board (4.5.0): the editable record + its mtime, the per-site tag
     * view, and recording a hand-posted copy by URL (preview, then confirm). */
    getStoryMetadata(name) { return this.get(`/api/editor/stories/${encodeURIComponent(name)}/metadata`); },
    // 409 = changed elsewhere; the board offers Reload in place.
    saveStoryMetadata(name, body) { return this.put(`/api/editor/stories/${encodeURIComponent(name)}/metadata`, body, { quiet: [409] }); },
    getStoryTagPreview(name) { return this.get(`/api/editor/stories/${encodeURIComponent(name)}/tag-preview`); },
    // 409 = that chapter is already recorded there; 422 = not a link it knows.
    linkStoryByUrl(name, body) { return this.post(`/api/posting/stories/${encodeURIComponent(name)}/link-url`, body, { quiet: [400, 409, 422] }); },
    /* Editor story text — used by the Promo Maker's "pull an excerpt" picker. */
    getEditorStories() { return this.get('/api/editor/stories'); },
    getEditorStoryContent(name) {
        return this.get(`/api/editor/stories/${encodeURIComponent(name)}/content`);
    },
    /* Beta-reader draft share (gap-wave-5 §3) — read-only public preview links. */
    createShareLink(name, expiresDays = null) {
        return this.post(`/api/editor/stories/${encodeURIComponent(name)}/share`,
            { expires_days: expiresDays });
    },
    listShareLinks(name) {
        return this.get(`/api/editor/stories/${encodeURIComponent(name)}/share`);
    },
    revokeShareLink(token) {
        return this.del(`/api/editor/share/${encodeURIComponent(token)}`);
    },
    postStory(data) { return this.post('/api/posting/post', data); },
    updateStory(data) { return this.post('/api/posting/update', data); },
    getPublications(params = {}) { return this.get('/api/posting/publications', params); },
    getPublicationsWithStats(params = {}) { return this.get('/api/posting/publications/stats', params); },
    getPublication(pubId) { return this.get(`/api/posting/publications/${pubId}`); },
    addToPostingQueue(data) { return this.post('/api/posting/queue', data); },
    getPostingQueue(params = {}) { return this.get('/api/posting/queue', params); },
    getClearableQueue() { return this.get('/api/posting/queue/clearable'); },
    clearPostingQueue(statuses) { return this.post('/api/posting/queue/clear', { statuses }); },
    cancelPostingQueue(queueId) {
        return fetch(`/api/posting/queue/${queueId}`, { method: 'DELETE' }).then(r => {
            if (!r.ok) throw new Error(`Cancel failed: ${r.status}`);
            return r.json();
        });
    },
    /* Move a scheduled queue item (story or artwork) to a new time.
       data = { scheduled_at: <ISO 8601 string> }. */
    reschedulePostingQueue(queueId, data) {
        return this.post(`/api/posting/queue/${queueId}/reschedule`, data);
    },
    getPostingLog(params = {}) { return this.get('/api/posting/log', params); },
    getPostingSettings() { return this.get('/api/posting/settings'); },
    savePostingSettings(data) { return this.post('/api/posting/settings', data); },
    syncPush(data = {}) { return this.post('/api/posting/sync/push', data); },
    getSyncStatus() { return this.get('/api/posting/sync/status'); },
    getPostingChanges() { return this.get('/api/posting/changes'); },
    claimSubmissions(data = {}) { return this.post('/api/posting/claim', data); },

    /* ── Submissions hub (unified works library) ──────────────── */
    getWorks(params = {}) { return this.get('/api/works', params); },
    getDiscovered(params = {}) { return this.get('/api/works/discovered', params); },
    ignoreDiscovered(platform, submissionId) {
        return this.post('/api/works/discovered/ignore', { platform, submission_id: submissionId });
    },
    unignoreDiscovered(platform, submissionId) {
        return fetch(`/api/works/discovered/ignore/${platform}/${encodeURIComponent(submissionId)}`,
            { method: 'DELETE' }).then(r => { if (!r.ok) throw new Error(`Un-ignore failed: ${r.status}`); return r.json(); });
    },
    getIgnoredDiscovered() { return this.get('/api/works/discovered/ignored'); },
    linkSubmission(body) { return this.post('/api/works/link', body); },
    importArtwork(platform, submissionId) {
        return this.post(`/api/artwork/import/${platform}/${encodeURIComponent(submissionId)}`);
    },
    importBulk(platform) { return this.post(`/api/artwork/import/bulk/${platform}`); },
    importDiscoveredArt() { return this.post('/api/artwork/import/discovered-art'); },
    /* Discovered TEXT posts → the Posts module (2.157.0). The artwork import
       above downloads an image; these have none — a tweet is a post. */
    importDiscoveredPost(platform, submissionId) {
        return this.post(`/api/posts/import/${platform}/${encodeURIComponent(submissionId)}`);
    },
    importDiscoveredPosts() { return this.post('/api/posts/import/discovered'); },

    /* ── Artwork Hub ──────────────────────────────────────────── */
    getArtworks() { return this.get('/api/artwork/images'); },
    getArtwork(name) { return this.get(`/api/artwork/images/${encodeURIComponent(name)}`); },
    updateArtwork(name, data) {
        return fetch(`/api/artwork/images/${encodeURIComponent(name)}`, {
            method: 'PATCH',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(data),
        }).then(r => { if (!r.ok) throw new Error(`Save failed: ${r.status}`); return r.json(); });
    },
    deleteArtwork(name) {
        return fetch(`/api/artwork/images/${encodeURIComponent(name)}`, { method: 'DELETE' })
            .then(r => { if (!r.ok) throw new Error(`Delete failed: ${r.status}`); return r.json(); });
    },
    uploadArtwork(file, metadata, thumbnail, onProgress) {
        const fd = new FormData();
        fd.append('file', file);
        fd.append('metadata', JSON.stringify(metadata || {}));
        if (thumbnail) fd.append('thumbnail', thumbnail);
        return this._upload('/api/artwork/upload', fd, onProgress);
    },
    createArtworkFromPath(data) { return this.post('/api/artwork/create-from-path', data); },
    publishArtwork(data) { return this.post('/api/artwork/publish', data); },
    /* Schedule an artwork to publish later. One call per platform.
       data = { artwork_name, platform, scheduled_at, account_id? }. */
    scheduleArtwork(data) { return this.post('/api/artwork/schedule', data); },
    getArtworkScheduled(name) { return this.get('/api/artwork/scheduled', { name }); },
    cancelArtworkScheduled(name, queueId) {
        return fetch(`/api/artwork/scheduled/${queueId}?name=${encodeURIComponent(name)}`,
            { method: 'DELETE' }).then(r => {
                if (!r.ok) throw new Error(`Cancel failed: ${r.status}`);
                return r.json();
            });
    },
    getArtworkPublications() { return this.get('/api/artwork/publications'); },
    getArtworkLog(params = {}) { return this.get('/api/artwork/log', params); },
    getArtworkSettings() { return this.get('/api/artwork/settings'); },
    saveArtworkSettings(data) { return this.post('/api/artwork/settings', data); },
    artworkSyncPush(data = {}) { return this.post('/api/artwork/sync/push', data); },

    /* ── Posts (microblog) module ─────────────────────────────── */
    getPosts() { return this.get('/api/posts'); },
    getPost(id) { return this.get(`/api/posts/${id}`); },
    createPost(formData) {
        // multipart (optional image rides along) — let the browser set the boundary.
        return fetch('/api/posts', { method: 'POST', body: formData })
            .then(async r => {
                const j = await r.json().catch(() => ({}));
                if (!r.ok) throw new Error(j.detail || `Create failed: ${r.status}`);
                return j;
            });
    },
    publishPost(id, body) { return this.post(`/api/posts/${id}/publish`, body); },
    /* Schedule a post for later. body = { platforms, account_ids?, scheduled_at }. */
    schedulePost(id, body) { return this.post(`/api/posts/${id}/schedule`, body); },
    deletePost(id) {
        return fetch(`/api/posts/${id}`, { method: 'DELETE' })
            .then(r => { if (!r.ok) throw new Error(`Delete failed: ${r.status}`); return r.json(); });
    },
    postImageUrl(id) { return `/api/posts/image?post_id=${encodeURIComponent(id)}`; },

    /* Handle-book (contacts) for @mentions — a person's per-platform handles. */
    getContacts() { return this.get('/api/posts/contacts'); },
    createContact(body) { return this.post('/api/posts/contacts', body); },
    updateContact(id, body) {
        return fetch(`/api/posts/contacts/${id}`, {
            method: 'PATCH', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
        }).then(async r => {
            const j = await r.json().catch(() => ({}));
            if (!r.ok) throw new Error(j.detail || `Update failed: ${r.status}`);
            return j;
        });
    },
    deleteContact(id) {
        return fetch(`/api/posts/contacts/${id}`, { method: 'DELETE' })
            .then(r => { if (!r.ok) throw new Error(`Delete failed: ${r.status}`); return r.json(); });
    },

    /* FormData upload with optional progress (XHR — fetch lacks upload progress). */
    _upload(path, formData, onProgress) {
        return new Promise((resolve, reject) => {
            const xhr = new XMLHttpRequest();
            if (onProgress) xhr.upload.addEventListener('progress', e => {
                if (e.lengthComputable) onProgress(Math.round((e.loaded / e.total) * 100));
            });
            xhr.addEventListener('load', () => {
                if (xhr.status >= 200 && xhr.status < 300) {
                    try { resolve(JSON.parse(xhr.responseText)); } catch { resolve({}); }
                } else {
                    reject(new Error(`Upload ${xhr.status}: ${xhr.responseText}`));
                }
            });
            xhr.addEventListener('error', () => reject(new Error('Network error during upload')));
            xhr.open('POST', path);
            xhr.send(formData);
        });
    },

    /* ── Setup wizard ─────────────────────────────────────────── */
    getSetupStatus() { return this.get('/api/settings/setup-status'); },
    markSetupComplete() { return this.post('/api/settings/setup-complete'); },
    setSetupMode(payload) { return this.post('/api/settings/setup-mode', payload); },
    pairTest(payload) { return this.post('/api/settings/pair-test', payload); },
    resetSetupWizard() { return this.post('/api/settings/setup-reset'); },

    /* ── Browser login (embedded pywebview popup) ────────────── */
    getBrowserLoginPlatforms() { return this.get('/api/settings/browser-login/platforms'); },
    browserLogin(platform, extraFields = {}) {
        return this.post(`/api/settings/browser-login/${platform}`, { extra_fields: extraFields });
    },
};
