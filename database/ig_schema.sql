-- Instagram (IG) Analytics Database Schema
--
-- Official Instagram Graph API (graph.instagram.com, the "Instagram API with
-- Instagram Login" flow), OAuth long-lived token.
-- Engagement from the media object (likes/comments) + per-media /insights
-- endpoint (views/reach/saved/shares).
--
-- Metrics: views, reach, likes, comments, saved, shares.
-- Post IDs are numeric strings (the media id) stored as TEXT.
--
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS ig_submissions (
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
    media_urls      TEXT DEFAULT '',
    views           INTEGER DEFAULT 0,
    reach           INTEGER DEFAULT 0,
    likes           INTEGER DEFAULT 0,
    comments        INTEGER DEFAULT 0,
    saved           INTEGER DEFAULT 0,
    shares          INTEGER DEFAULT 0,
    has_media       INTEGER DEFAULT 0,
    embed_type      TEXT DEFAULT '',
    updated_at      TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS ig_snapshots (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    submission_id   TEXT NOT NULL,
    polled_at       TEXT NOT NULL DEFAULT (datetime('now')),
    views           INTEGER NOT NULL DEFAULT 0,
    reach           INTEGER NOT NULL DEFAULT 0,
    likes           INTEGER NOT NULL DEFAULT 0,
    comments        INTEGER NOT NULL DEFAULT 0,
    saved           INTEGER NOT NULL DEFAULT 0,
    shares          INTEGER NOT NULL DEFAULT 0,
    FOREIGN KEY (submission_id) REFERENCES ig_submissions(submission_id)
);

CREATE INDEX IF NOT EXISTS idx_ig_snapshots_submission_polled
    ON ig_snapshots(submission_id, polled_at);

CREATE INDEX IF NOT EXISTS idx_ig_snapshots_polled
    ON ig_snapshots(polled_at);

CREATE TABLE IF NOT EXISTS ig_poll_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at      TEXT NOT NULL DEFAULT (datetime('now')),
    finished_at     TEXT,
    status          TEXT NOT NULL DEFAULT 'running',
    submissions_found INTEGER DEFAULT 0,
    snapshots_inserted INTEGER DEFAULT 0,
    error_message   TEXT,
    duration_seconds REAL
);
