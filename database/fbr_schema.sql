-- Furbooru (fbr) Analytics Database Schema
--
-- Furbooru runs the Philomena booru engine. Poll-only via the public read JSON
-- API (no auth needed for public uploads). Booru metric shape (same as e621):
-- SCORE (upvotes − downvotes, can be NEGATIVE) with the up/down split trended,
-- favorites_count (faves), comments_count (comment_count). No view count.
-- submission_id is the Philomena image id as TEXT. account_id is in the initial
-- schema (fresh platform).
--
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS fbr_submissions (
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
    score           INTEGER DEFAULT 0,
    up_score        INTEGER DEFAULT 0,
    down_score      INTEGER DEFAULT 0,
    favorites_count INTEGER DEFAULT 0,
    comments_count  INTEGER DEFAULT 0,
    has_media       INTEGER DEFAULT 0,
    updated_at      TEXT DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_fbr_submissions_account ON fbr_submissions(account_id);

CREATE TABLE IF NOT EXISTS fbr_snapshots (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id      INTEGER NOT NULL DEFAULT 0,
    submission_id   TEXT NOT NULL,
    polled_at       TEXT NOT NULL DEFAULT (datetime('now')),
    score           INTEGER NOT NULL DEFAULT 0,
    up_score        INTEGER NOT NULL DEFAULT 0,
    down_score      INTEGER NOT NULL DEFAULT 0,
    favorites_count INTEGER NOT NULL DEFAULT 0,
    comments_count  INTEGER NOT NULL DEFAULT 0,
    FOREIGN KEY (submission_id) REFERENCES fbr_submissions(submission_id)
);

CREATE INDEX IF NOT EXISTS idx_fbr_snapshots_submission_polled
    ON fbr_snapshots(submission_id, polled_at);
CREATE INDEX IF NOT EXISTS idx_fbr_snapshots_polled ON fbr_snapshots(polled_at);

CREATE TABLE IF NOT EXISTS fbr_poll_log (
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
