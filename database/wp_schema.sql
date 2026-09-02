-- Wattpad (WP) Analytics Database Schema
--
-- Wattpad provides a public JSON API at api.wattpad.com.
-- No authentication required — only a username is needed.
--
-- Stats tracked: reads, votes, comments, reading lists (num_lists).
-- Reading lists is unique to Wattpad among PawPoller platforms.
-- Story IDs are integers.
--
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS wp_submissions (
    submission_id   INTEGER PRIMARY KEY,
    title           TEXT NOT NULL DEFAULT '',
    username        TEXT NOT NULL DEFAULT '',
    posted_at       TEXT,
    category        TEXT DEFAULT '',
    rating          TEXT DEFAULT '',
    description     TEXT DEFAULT '',
    keywords        TEXT DEFAULT '',
    link            TEXT DEFAULT '',
    cover_url       TEXT DEFAULT '',
    word_count      INTEGER DEFAULT 0,
    num_parts       INTEGER DEFAULT 0,
    completed       INTEGER DEFAULT 0,
    reads           INTEGER DEFAULT 0,
    votes           INTEGER DEFAULT 0,
    comments_count  INTEGER DEFAULT 0,
    num_lists       INTEGER DEFAULT 0,
    updated_at      TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS wp_snapshots (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    submission_id   INTEGER NOT NULL,
    polled_at       TEXT NOT NULL DEFAULT (datetime('now')),
    reads           INTEGER NOT NULL DEFAULT 0,
    votes           INTEGER NOT NULL DEFAULT 0,
    comments_count  INTEGER NOT NULL DEFAULT 0,
    num_lists       INTEGER NOT NULL DEFAULT 0,
    FOREIGN KEY (submission_id) REFERENCES wp_submissions(submission_id)
);

CREATE INDEX IF NOT EXISTS idx_wp_snapshots_submission_polled
    ON wp_snapshots(submission_id, polled_at);

CREATE INDEX IF NOT EXISTS idx_wp_snapshots_polled
    ON wp_snapshots(polled_at);

CREATE TABLE IF NOT EXISTS wp_poll_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at      TEXT NOT NULL DEFAULT (datetime('now')),
    finished_at     TEXT,
    status          TEXT NOT NULL DEFAULT 'running',
    submissions_found INTEGER DEFAULT 0,
    snapshots_inserted INTEGER DEFAULT 0,
    error_message   TEXT,
    duration_seconds REAL
);
