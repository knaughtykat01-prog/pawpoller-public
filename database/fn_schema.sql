-- FurryNetwork (fn) Analytics Database Schema
--
-- Poll+post gallery. OAuth2 (email+password → token). Tracks the connected
-- user's own submissions across their FN "characters" and snapshots engagement
-- over time. Standard gallery metric shape: views / favorites_count /
-- comments_count (unlike e621's score). submission_id is the FN submission id
-- as TEXT; the character the work belongs to is stored in `username`.
--
-- account_id is included in the initial schema (fresh platform, no migration
-- needed) — the poller writes it on every upsert and multi-account resolution
-- reads it.
--
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS fn_submissions (
    submission_id   TEXT PRIMARY KEY,
    account_id      INTEGER NOT NULL DEFAULT 0,
    title           TEXT NOT NULL DEFAULT '',
    full_text       TEXT DEFAULT '',
    username        TEXT NOT NULL DEFAULT '',
    posted_at       TEXT,
    content_type    TEXT DEFAULT 'image',
    rating          TEXT DEFAULT '',
    description     TEXT DEFAULT '',
    keywords        TEXT DEFAULT '',
    link            TEXT DEFAULT '',
    thumbnail_url   TEXT DEFAULT '',
    file_url        TEXT DEFAULT '',
    views           INTEGER DEFAULT 0,
    favorites_count INTEGER DEFAULT 0,
    comments_count  INTEGER DEFAULT 0,
    has_media       INTEGER DEFAULT 0,
    updated_at      TEXT DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_fn_submissions_account ON fn_submissions(account_id);

CREATE TABLE IF NOT EXISTS fn_snapshots (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id      INTEGER NOT NULL DEFAULT 0,
    submission_id   TEXT NOT NULL,
    polled_at       TEXT NOT NULL DEFAULT (datetime('now')),
    views           INTEGER NOT NULL DEFAULT 0,
    favorites_count INTEGER NOT NULL DEFAULT 0,
    comments_count  INTEGER NOT NULL DEFAULT 0,
    FOREIGN KEY (submission_id) REFERENCES fn_submissions(submission_id)
);

CREATE INDEX IF NOT EXISTS idx_fn_snapshots_submission_polled
    ON fn_snapshots(submission_id, polled_at);
CREATE INDEX IF NOT EXISTS idx_fn_snapshots_polled ON fn_snapshots(polled_at);

CREATE TABLE IF NOT EXISTS fn_poll_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id      INTEGER NOT NULL DEFAULT 0,
    started_at      TEXT NOT NULL DEFAULT (datetime('now')),
    finished_at     TEXT,
    status          TEXT NOT NULL DEFAULT 'running',
    submissions_found INTEGER DEFAULT 0,
    snapshots_inserted INTEGER DEFAULT 0,
    error_message   TEXT,
    duration_seconds REAL
);
