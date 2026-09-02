-- SoFurry Analytics Database Schema
--
-- SoFurry's new platform ("SoFurry Next") does not provide a public API or
-- API key generation.  Data is collected by scraping the web interface after
-- authenticating with email/password to obtain session cookies.
--
-- This schema closely mirrors the Weasyl schema (ws_schema.sql) since the
-- available data is similar: views, likes (mapped to favorites_count), and
-- comments count.  SoFurry uses alphanumeric submission IDs (e.g. "nZ7RvxM1")
-- stored as TEXT rather than INTEGER.
--
-- KEY DIFFERENCES FROM OTHER PLATFORMS:
--   - submission_id is TEXT (alphanumeric slug), not INTEGER
--   - "likes" on SoFurry map to favorites_count for consistency
--   - No individual comment data available (count only, like Weasyl)
--   - No faving-user data available (count only)
--   - content_type instead of type_name/subtype: "Artwork", "Writing", etc.
--
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

-- One row per SoFurry submission. Created when first discovered via gallery
-- scraping. Updated on subsequent polls with latest metadata and stats.
CREATE TABLE IF NOT EXISTS sf_submissions (
    submission_id   TEXT PRIMARY KEY,       -- SoFurry's alphanumeric ID (e.g. "nZ7RvxM1")
    title           TEXT NOT NULL DEFAULT '',
    username        TEXT NOT NULL DEFAULT '',
    posted_at       TEXT,                   -- Relative date from page, stored as-is
    content_type    TEXT DEFAULT '',        -- "Artwork", "Writing", "Music", etc.
    rating          TEXT DEFAULT '',        -- "Clean", "Mature", "Adult"
    thumbnail_url   TEXT DEFAULT '',
    description     TEXT DEFAULT '',
    keywords        TEXT DEFAULT '',        -- JSON array of tag strings
    link            TEXT DEFAULT '',        -- Direct URL to the SoFurry submission page
    views           INTEGER DEFAULT 0,
    favorites_count INTEGER DEFAULT 0,     -- "Likes" on SoFurry
    comments_count  INTEGER DEFAULT 0,
    updated_at      TEXT DEFAULT (datetime('now'))
);

-- Time-series stats. One row per poll cycle per submission.
CREATE TABLE IF NOT EXISTS sf_snapshots (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    submission_id   TEXT NOT NULL,
    polled_at       TEXT NOT NULL DEFAULT (datetime('now')),
    views           INTEGER NOT NULL DEFAULT 0,
    favorites_count INTEGER NOT NULL DEFAULT 0,
    comments_count  INTEGER NOT NULL DEFAULT 0,
    FOREIGN KEY (submission_id) REFERENCES sf_submissions(submission_id)
);

CREATE INDEX IF NOT EXISTS idx_sf_snapshots_submission_polled
    ON sf_snapshots(submission_id, polled_at);

CREATE INDEX IF NOT EXISTS idx_sf_snapshots_polled
    ON sf_snapshots(polled_at);

-- Audit log for SoFurry poll cycles.
CREATE TABLE IF NOT EXISTS sf_poll_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at      TEXT NOT NULL DEFAULT (datetime('now')),
    finished_at     TEXT,
    status          TEXT NOT NULL DEFAULT 'running',
    submissions_found INTEGER DEFAULT 0,
    snapshots_inserted INTEGER DEFAULT 0,
    new_watchers_found INTEGER DEFAULT 0,
    error_message   TEXT,
    duration_seconds REAL
);

-- Follower tracking.
CREATE TABLE IF NOT EXISTS sf_watchers (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    username      TEXT NOT NULL,
    first_seen_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(username)
);
