-- Bluesky (BSKY) Analytics Database Schema
--
-- Bluesky provides a free public API via the AT Protocol.
-- Authentication uses app passwords to obtain JWT sessions.
--
-- Stats tracked: likes, reposts, replies, quotes.
-- Bluesky does NOT provide view/impression counts.
-- Post IDs are AT URIs (at://did:plc:xxx/app.bsky.feed.post/yyy) stored as TEXT.
--
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS bsky_submissions (
    submission_id   TEXT PRIMARY KEY,
    title           TEXT NOT NULL DEFAULT '',
    full_text       TEXT DEFAULT '',
    username        TEXT NOT NULL DEFAULT '',
    posted_at       TEXT,
    content_type    TEXT DEFAULT 'post',
    rating          TEXT DEFAULT '',
    description     TEXT DEFAULT '',
    keywords        TEXT DEFAULT '',
    link            TEXT DEFAULT '',
    thumbnail_url   TEXT DEFAULT '',
    media_urls      TEXT DEFAULT '',
    likes           INTEGER DEFAULT 0,
    reposts         INTEGER DEFAULT 0,
    replies         INTEGER DEFAULT 0,
    quotes          INTEGER DEFAULT 0,
    has_media       INTEGER DEFAULT 0,
    embed_type      TEXT DEFAULT '',
    updated_at      TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS bsky_snapshots (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    submission_id   TEXT NOT NULL,
    polled_at       TEXT NOT NULL DEFAULT (datetime('now')),
    likes           INTEGER NOT NULL DEFAULT 0,
    reposts         INTEGER NOT NULL DEFAULT 0,
    replies         INTEGER NOT NULL DEFAULT 0,
    quotes          INTEGER NOT NULL DEFAULT 0,
    FOREIGN KEY (submission_id) REFERENCES bsky_submissions(submission_id)
);

CREATE INDEX IF NOT EXISTS idx_bsky_snapshots_submission_polled
    ON bsky_snapshots(submission_id, polled_at);

CREATE INDEX IF NOT EXISTS idx_bsky_snapshots_polled
    ON bsky_snapshots(polled_at);

CREATE TABLE IF NOT EXISTS bsky_poll_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at      TEXT NOT NULL DEFAULT (datetime('now')),
    finished_at     TEXT,
    status          TEXT NOT NULL DEFAULT 'running',
    submissions_found INTEGER DEFAULT 0,
    snapshots_inserted INTEGER DEFAULT 0,
    error_message   TEXT,
    duration_seconds REAL
);
