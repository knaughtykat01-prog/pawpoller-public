-- SquidgeWorld Analytics Database Schema
--
-- SquidgeWorld runs the OTW Archive open-source software (same as AO3).
-- Data is collected by authenticating with a login account and scraping
-- work pages, since there is no public API.
--
-- Stats tracked: hits (views), kudos (favorites), comments, bookmarks.
-- Work IDs are integers. Bookmarks are unique to OTW Archive platforms
-- and not tracked on Inkbunny, FA, Weasyl, or SoFurry.
--
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

-- One row per SquidgeWorld work. Created when first discovered via the
-- target user's works page. Updated on subsequent polls with latest stats.
CREATE TABLE IF NOT EXISTS sqw_submissions (
    submission_id   INTEGER PRIMARY KEY,    -- OTW Archive work ID (e.g. 88335)
    title           TEXT NOT NULL DEFAULT '',
    username        TEXT NOT NULL DEFAULT '',
    posted_at       TEXT,                   -- Date published (YYYY-MM-DD)
    fandom          TEXT DEFAULT '',        -- Primary fandom tag
    rating          TEXT DEFAULT '',        -- "General Audiences", "Explicit", etc.
    description     TEXT DEFAULT '',        -- Work summary text
    keywords        TEXT DEFAULT '',        -- JSON array of tag strings
    link            TEXT DEFAULT '',        -- Full URL to the work page
    word_count      INTEGER DEFAULT 0,
    chapters        TEXT DEFAULT '1/1',     -- e.g. "5/5" or "3/?"
    views           INTEGER DEFAULT 0,     -- "Hits" on OTW Archive
    favorites_count INTEGER DEFAULT 0,     -- "Kudos" on OTW Archive
    comments_count  INTEGER DEFAULT 0,
    bookmarks_count INTEGER DEFAULT 0,     -- Unique to OTW Archive
    updated_at      TEXT DEFAULT (datetime('now'))
);

-- Time-series stats. One row per poll cycle per work.
CREATE TABLE IF NOT EXISTS sqw_snapshots (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    submission_id   INTEGER NOT NULL,
    polled_at       TEXT NOT NULL DEFAULT (datetime('now')),
    views           INTEGER NOT NULL DEFAULT 0,
    favorites_count INTEGER NOT NULL DEFAULT 0,
    comments_count  INTEGER NOT NULL DEFAULT 0,
    bookmarks_count INTEGER NOT NULL DEFAULT 0,
    FOREIGN KEY (submission_id) REFERENCES sqw_submissions(submission_id)
);

CREATE INDEX IF NOT EXISTS idx_sqw_snapshots_submission_polled
    ON sqw_snapshots(submission_id, polled_at);

CREATE INDEX IF NOT EXISTS idx_sqw_snapshots_polled
    ON sqw_snapshots(polled_at);

-- Audit log for SquidgeWorld poll cycles.
CREATE TABLE IF NOT EXISTS sqw_poll_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at      TEXT NOT NULL DEFAULT (datetime('now')),
    finished_at     TEXT,
    status          TEXT NOT NULL DEFAULT 'running',
    submissions_found INTEGER DEFAULT 0,
    snapshots_inserted INTEGER DEFAULT 0,
    new_kudos_found INTEGER DEFAULT 0,
    error_message   TEXT,
    duration_seconds REAL
);

-- Kudos user tracking — who left kudos on each work.
CREATE TABLE IF NOT EXISTS sqw_kudos_users (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    submission_id   INTEGER NOT NULL,
    username        TEXT NOT NULL DEFAULT '',
    first_seen_at   TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (submission_id) REFERENCES sqw_submissions(submission_id),
    UNIQUE(submission_id, username)
);

CREATE INDEX IF NOT EXISTS idx_sqw_kudos_submission
    ON sqw_kudos_users(submission_id);

CREATE INDEX IF NOT EXISTS idx_sqw_kudos_seen
    ON sqw_kudos_users(first_seen_at);
