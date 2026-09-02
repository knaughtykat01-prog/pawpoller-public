-- Weasyl Analytics Database Schema
--
-- Differences from the Inkbunny (IB) schema (schema.sql):
--
-- SUBMISSION METADATA:
--   - subtype instead of type_name: Weasyl classifies submissions by "subtype"
--     (e.g. "visual", "literary", "multimedia") rather than IB's type_name
--     ("Picture/Pinup", "Writing", etc).
--   - media_url instead of url: Weasyl's full-resolution file is accessed via
--     a nested media JSON structure (media.submission[0].url). Stored here as
--     a flat string after extraction. IB uses "url"; FA uses "download_url".
--   - No category/theme/species/gender: Weasyl does not have FA's structured
--     classification system. Content is classified only by subtype + keywords.
--   - No user_id / page_count / rating_id / rating_name: Weasyl's API provides
--     fewer metadata fields than IB. Rating is text-only ("general", "mature",
--     "explicit") like FA, not numeric like IB.
--   - posted_at instead of create_datetime: different timestamp field name.
--   - link: direct URL to the Weasyl submission page.
--
-- COMMENTS:
--   - NO ws_comments table: the Weasyl API does not expose individual comment
--     text, usernames, or threading. Only an aggregate comment count is
--     available on each submission. This is a fundamental API limitation --
--     IB provides full comment data, and FA provides it via scraping, but
--     Weasyl provides none.
--
-- FAVING USERS:
--   - No ws_faving_users table: like FA, Weasyl does not expose per-user
--     favourite data through its API. Only aggregate counts are tracked.
--
-- POLL LOG:
--   - No new_faves_found or new_comments_found columns: since Weasyl exposes
--     neither individual faves nor individual comments, the poll log only
--     tracks submissions_found and snapshots_inserted.
--
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

-- ── ws_submissions ──────────────────────────────────────────────────────────
-- One row per Weasyl submission. Created when first discovered via the gallery
-- API. Updated on subsequent polls with latest metadata and stats.
-- This is the simplest of the three platform schemas due to Weasyl's more
-- limited API surface.
CREATE TABLE IF NOT EXISTS ws_submissions (
    submission_id   INTEGER PRIMARY KEY,    -- Weasyl's native submission ID ("submitid" in API)
    title           TEXT NOT NULL DEFAULT '',
    username        TEXT NOT NULL DEFAULT '',
    posted_at       TEXT,                   -- ISO timestamp from Weasyl API
    subtype         TEXT DEFAULT '',        -- Weasyl content type: "visual", "literary", "multimedia"
    rating          TEXT DEFAULT '',        -- "general", "mature", or "explicit" (text, not numeric)
    thumbnail_url   TEXT DEFAULT '',        -- Extracted from media.thumbnail[0].url in API JSON
    media_url       TEXT DEFAULT '',        -- Extracted from media.submission[0].url (full-res file)
    description     TEXT DEFAULT '',        -- Submission description (may contain HTML)
    keywords        TEXT DEFAULT '',        -- JSON array of tag strings (same format as IB/FA)
    link            TEXT DEFAULT '',        -- Direct URL to the Weasyl submission page
    -- Denormalized latest stats (same pattern as IB/FA -- see schema.sql for rationale).
    views           INTEGER DEFAULT 0,
    favorites_count INTEGER DEFAULT 0,
    comments_count  INTEGER DEFAULT 0,      -- Count only; no individual comment data available
    updated_at      TEXT DEFAULT (datetime('now'))
);

-- ── ws_snapshots ────────────────────────────────────────────────────────────
-- Time-series stats, same pattern as IB/FA snapshots. One row per poll cycle
-- per submission. comments_count is tracked here even though individual
-- comments are not available -- the count can still show trends over time.
CREATE TABLE IF NOT EXISTS ws_snapshots (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    submission_id   INTEGER NOT NULL,
    polled_at       TEXT NOT NULL DEFAULT (datetime('now')),
    views           INTEGER NOT NULL DEFAULT 0,
    favorites_count INTEGER NOT NULL DEFAULT 0,
    comments_count  INTEGER NOT NULL DEFAULT 0,
    FOREIGN KEY (submission_id) REFERENCES ws_submissions(submission_id)
);

-- Composite index for time-series queries on a specific submission.
CREATE INDEX IF NOT EXISTS idx_ws_snapshots_submission_polled
    ON ws_snapshots(submission_id, polled_at);

-- Index for "all snapshots in a time range" queries.
CREATE INDEX IF NOT EXISTS idx_ws_snapshots_polled
    ON ws_snapshots(polled_at);

-- ── ws_poll_log ─────────────────────────────────────────────────────────────
-- Audit log for Weasyl poll cycles. Simpler than IB/FA poll logs because
-- Weasyl's API does not expose individual faves or comments, so there are no
-- new_faves_found or new_comments_found columns to track.
CREATE TABLE IF NOT EXISTS ws_poll_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at      TEXT NOT NULL DEFAULT (datetime('now')),
    finished_at     TEXT,
    status          TEXT NOT NULL DEFAULT 'running',
    submissions_found INTEGER DEFAULT 0,
    snapshots_inserted INTEGER DEFAULT 0,
    -- No new_faves_found: Weasyl API does not expose per-user favourite data.
    -- No new_comments_found: Weasyl API does not expose individual comments.
    error_message   TEXT,
    duration_seconds REAL
);
