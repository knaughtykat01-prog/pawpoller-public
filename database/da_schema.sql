-- DeviantArt (DA) Analytics Database Schema
--
-- DeviantArt uses the Eclipse frontend with internal _napi endpoints.
-- Data is collected via cookie-based authentication.
--
-- Stats tracked: views, favourites, comments, downloads.
-- Downloads is unique to DeviantArt among PawPoller platforms.
-- Deviation IDs are integers.
--
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS da_submissions (
    submission_id   INTEGER PRIMARY KEY,
    title           TEXT NOT NULL DEFAULT '',
    username        TEXT NOT NULL DEFAULT '',
    posted_at       TEXT,
    category        TEXT DEFAULT '',
    rating          TEXT DEFAULT '',
    description     TEXT DEFAULT '',
    keywords        TEXT DEFAULT '',
    link            TEXT DEFAULT '',
    thumbnail_url   TEXT DEFAULT '',
    views           INTEGER DEFAULT 0,
    favorites_count INTEGER DEFAULT 0,
    comments_count  INTEGER DEFAULT 0,
    downloads       INTEGER DEFAULT 0,
    updated_at      TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS da_snapshots (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    submission_id   INTEGER NOT NULL,
    polled_at       TEXT NOT NULL DEFAULT (datetime('now')),
    views           INTEGER NOT NULL DEFAULT 0,
    favorites_count INTEGER NOT NULL DEFAULT 0,
    comments_count  INTEGER NOT NULL DEFAULT 0,
    downloads       INTEGER NOT NULL DEFAULT 0,
    FOREIGN KEY (submission_id) REFERENCES da_submissions(submission_id)
);

CREATE INDEX IF NOT EXISTS idx_da_snapshots_submission_polled
    ON da_snapshots(submission_id, polled_at);

CREATE INDEX IF NOT EXISTS idx_da_snapshots_polled
    ON da_snapshots(polled_at);

CREATE TABLE IF NOT EXISTS da_poll_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at      TEXT NOT NULL DEFAULT (datetime('now')),
    finished_at     TEXT,
    status          TEXT NOT NULL DEFAULT 'running',
    submissions_found INTEGER DEFAULT 0,
    snapshots_inserted INTEGER DEFAULT 0,
    error_message   TEXT,
    duration_seconds REAL
);
