-- Pixiv (PIX) Analytics Database Schema
--
-- Reverse-engineered app-API (pixivpy-style), OAuth via a refresh token.
-- Illustrations + novels share the same engagement shape, so both are tracked.
--
-- Metrics map to the gallery shape: views (total_view),
-- favorites_count (total_bookmarks), comments_count (total_comments).
-- Work IDs are namespaced numeric strings ("illust:123" / "novel:123") as TEXT.
--
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS pix_submissions (
    submission_id   TEXT PRIMARY KEY,
    title           TEXT NOT NULL DEFAULT '',
    full_text       TEXT DEFAULT '',
    username        TEXT NOT NULL DEFAULT '',
    posted_at       TEXT,
    content_type    TEXT DEFAULT 'illust',
    rating          TEXT DEFAULT '',
    description     TEXT DEFAULT '',
    keywords        TEXT DEFAULT '',
    link            TEXT DEFAULT '',
    thumbnail_url   TEXT DEFAULT '',
    views           INTEGER DEFAULT 0,
    favorites_count INTEGER DEFAULT 0,
    comments_count  INTEGER DEFAULT 0,
    has_media       INTEGER DEFAULT 0,
    embed_type      TEXT DEFAULT '',
    updated_at      TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS pix_snapshots (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    submission_id   TEXT NOT NULL,
    polled_at       TEXT NOT NULL DEFAULT (datetime('now')),
    views           INTEGER NOT NULL DEFAULT 0,
    favorites_count INTEGER NOT NULL DEFAULT 0,
    comments_count  INTEGER NOT NULL DEFAULT 0,
    FOREIGN KEY (submission_id) REFERENCES pix_submissions(submission_id)
);

CREATE INDEX IF NOT EXISTS idx_pix_snapshots_submission_polled
    ON pix_snapshots(submission_id, polled_at);

CREATE INDEX IF NOT EXISTS idx_pix_snapshots_polled
    ON pix_snapshots(polled_at);

CREATE TABLE IF NOT EXISTS pix_poll_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at      TEXT NOT NULL DEFAULT (datetime('now')),
    finished_at     TEXT,
    status          TEXT NOT NULL DEFAULT 'running',
    submissions_found INTEGER DEFAULT 0,
    snapshots_inserted INTEGER DEFAULT 0,
    error_message   TEXT,
    duration_seconds REAL
);
