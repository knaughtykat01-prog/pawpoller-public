-- Tumblr (TUM) Analytics Database Schema
--
-- Read-only polling via the Tumblr v2 API using the app's OAuth consumer key
-- (API key) + a blog identifier.
--
-- Engagement metric: notes (note_count = likes + reblogs + replies combined).
-- Tumblr does NOT expose a reliable per-post breakdown, so only the total is
-- tracked. Post IDs are numeric strings (id_string) stored as TEXT.
--
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS tum_submissions (
    submission_id   TEXT PRIMARY KEY,
    title           TEXT NOT NULL DEFAULT '',
    full_text       TEXT DEFAULT '',
    username        TEXT NOT NULL DEFAULT '',
    posted_at       TEXT,
    content_type    TEXT DEFAULT 'text',
    rating          TEXT DEFAULT '',
    description     TEXT DEFAULT '',
    keywords        TEXT DEFAULT '',
    link            TEXT DEFAULT '',
    thumbnail_url   TEXT DEFAULT '',
    notes           INTEGER DEFAULT 0,
    has_media       INTEGER DEFAULT 0,
    embed_type      TEXT DEFAULT '',
    updated_at      TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS tum_snapshots (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    submission_id   TEXT NOT NULL,
    polled_at       TEXT NOT NULL DEFAULT (datetime('now')),
    notes           INTEGER NOT NULL DEFAULT 0,
    FOREIGN KEY (submission_id) REFERENCES tum_submissions(submission_id)
);

CREATE INDEX IF NOT EXISTS idx_tum_snapshots_submission_polled
    ON tum_snapshots(submission_id, polled_at);

CREATE INDEX IF NOT EXISTS idx_tum_snapshots_polled
    ON tum_snapshots(polled_at);

CREATE TABLE IF NOT EXISTS tum_poll_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at      TEXT NOT NULL DEFAULT (datetime('now')),
    finished_at     TEXT,
    status          TEXT NOT NULL DEFAULT 'running',
    submissions_found INTEGER DEFAULT 0,
    snapshots_inserted INTEGER DEFAULT 0,
    error_message   TEXT,
    duration_seconds REAL
);
