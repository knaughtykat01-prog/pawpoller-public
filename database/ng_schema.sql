-- Newgrounds (ng) Analytics Database Schema — MEDIAPLATS §5 (4.23.0)
--
-- Poll+post, cookie session, HTML scraped (no API). Two portals — audio
-- (Listens) and movies (Views) — share one table; `portal` says which.
-- Standard gallery metric shape: views (Listens / Views) / favorites_count
-- (Faves) / comments_count (reviews — not in the stats block, stays 0).
-- Newgrounds' own score (x / 5.00), votes and downloads ride on the row.
-- submission_id is the site's numeric id as TEXT (the audio and movie id
-- spaces are separate, so `portal` is part of the identity in the link).
--
-- account_id is in the initial schema (fresh platform, no migration needed).
--
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS ng_submissions (
    submission_id   TEXT PRIMARY KEY,
    account_id      INTEGER NOT NULL DEFAULT 0,
    portal          TEXT NOT NULL DEFAULT 'audio',
    title           TEXT NOT NULL DEFAULT '',
    description     TEXT DEFAULT '',
    username        TEXT NOT NULL DEFAULT '',
    rating          TEXT DEFAULT '',
    genre           TEXT DEFAULT '',
    link            TEXT DEFAULT '',
    thumbnail_url   TEXT DEFAULT '',
    posted_at       TEXT,
    views           INTEGER DEFAULT 0,
    favorites_count INTEGER DEFAULT 0,
    comments_count  INTEGER DEFAULT 0,
    downloads_count INTEGER DEFAULT 0,
    votes           INTEGER DEFAULT 0,
    score           REAL DEFAULT 0,
    updated_at      TEXT DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_ng_submissions_account ON ng_submissions(account_id);

CREATE TABLE IF NOT EXISTS ng_snapshots (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id      INTEGER NOT NULL DEFAULT 0,
    submission_id   TEXT NOT NULL,
    polled_at       TEXT NOT NULL DEFAULT (datetime('now')),
    views           INTEGER NOT NULL DEFAULT 0,
    favorites_count INTEGER NOT NULL DEFAULT 0,
    comments_count  INTEGER NOT NULL DEFAULT 0,
    score           REAL DEFAULT 0,
    votes           INTEGER DEFAULT 0,
    downloads_count INTEGER DEFAULT 0,
    FOREIGN KEY (submission_id) REFERENCES ng_submissions(submission_id)
);

CREATE INDEX IF NOT EXISTS idx_ng_snapshots_submission_polled
    ON ng_snapshots(submission_id, polled_at);
CREATE INDEX IF NOT EXISTS idx_ng_snapshots_polled ON ng_snapshots(polled_at);

CREATE TABLE IF NOT EXISTS ng_poll_log (
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
