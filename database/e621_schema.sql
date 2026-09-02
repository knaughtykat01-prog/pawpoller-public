-- e621 (E621) Analytics Database Schema
--
-- Official e621 REST API (https://e621.net/posts.json), HTTP Basic auth
-- (username + API key). Poll-only: tracks the connected user's own uploads
-- (tags=user:<username>) and snapshots their engagement over time.
--
-- Metric shape mirrors the gallery platforms but the headline metric is
-- SCORE (score.total, which can be NEGATIVE) rather than views — e621 exposes
-- no view count. favorites_count = fav_count, comments_count = comment_count.
-- up_score / down_score (the vote split behind the net score) are stored on the
-- submission AND trended in each snapshot.
-- submission_id is the e621 post id as TEXT.
--
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS e621_submissions (
    submission_id   TEXT PRIMARY KEY,
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

CREATE TABLE IF NOT EXISTS e621_snapshots (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    submission_id   TEXT NOT NULL,
    polled_at       TEXT NOT NULL DEFAULT (datetime('now')),
    score           INTEGER NOT NULL DEFAULT 0,
    up_score        INTEGER NOT NULL DEFAULT 0,
    down_score      INTEGER NOT NULL DEFAULT 0,
    favorites_count INTEGER NOT NULL DEFAULT 0,
    comments_count  INTEGER NOT NULL DEFAULT 0,
    FOREIGN KEY (submission_id) REFERENCES e621_submissions(submission_id)
);

CREATE INDEX IF NOT EXISTS idx_e621_snapshots_submission_polled
    ON e621_snapshots(submission_id, polled_at);

CREATE INDEX IF NOT EXISTS idx_e621_snapshots_polled
    ON e621_snapshots(polled_at);

CREATE TABLE IF NOT EXISTS e621_poll_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at      TEXT NOT NULL DEFAULT (datetime('now')),
    finished_at     TEXT,
    status          TEXT NOT NULL DEFAULT 'running',
    submissions_found INTEGER DEFAULT 0,
    snapshots_inserted INTEGER DEFAULT 0,
    error_message   TEXT,
    duration_seconds REAL
);
