-- X/Twitter (TW) Analytics Database Schema
--
-- X/Twitter uses internal GraphQL endpoints with cookie-based auth.
-- Same cookie-based scraping approach as the DeviantArt integration.
--
-- Stats tracked: views, likes, retweets, replies, quotes, bookmarks (6 metrics).
-- Tweet IDs are numeric strings stored as TEXT (64-bit ints exceed JS safe range).
--
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS tw_submissions (
    submission_id   TEXT PRIMARY KEY,
    title           TEXT NOT NULL DEFAULT '',
    username        TEXT NOT NULL DEFAULT '',
    posted_at       TEXT,
    content_type    TEXT DEFAULT 'tweet',
    rating          TEXT DEFAULT '',
    description     TEXT DEFAULT '',
    keywords        TEXT DEFAULT '',
    link            TEXT DEFAULT '',
    thumbnail_url   TEXT DEFAULT '',
    media_urls      TEXT DEFAULT '',
    views           INTEGER DEFAULT 0,
    likes           INTEGER DEFAULT 0,
    retweets        INTEGER DEFAULT 0,
    replies         INTEGER DEFAULT 0,
    quotes          INTEGER DEFAULT 0,
    bookmarks       INTEGER DEFAULT 0,
    updated_at      TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS tw_snapshots (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    submission_id   TEXT NOT NULL,
    polled_at       TEXT NOT NULL DEFAULT (datetime('now')),
    views           INTEGER NOT NULL DEFAULT 0,
    likes           INTEGER NOT NULL DEFAULT 0,
    retweets        INTEGER NOT NULL DEFAULT 0,
    replies         INTEGER NOT NULL DEFAULT 0,
    quotes          INTEGER NOT NULL DEFAULT 0,
    bookmarks       INTEGER NOT NULL DEFAULT 0,
    FOREIGN KEY (submission_id) REFERENCES tw_submissions(submission_id)
);

CREATE INDEX IF NOT EXISTS idx_tw_snapshots_submission_polled
    ON tw_snapshots(submission_id, polled_at);

CREATE INDEX IF NOT EXISTS idx_tw_snapshots_polled
    ON tw_snapshots(polled_at);

CREATE TABLE IF NOT EXISTS tw_poll_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at      TEXT NOT NULL DEFAULT (datetime('now')),
    finished_at     TEXT,
    status          TEXT NOT NULL DEFAULT 'running',
    submissions_found INTEGER DEFAULT 0,
    snapshots_inserted INTEGER DEFAULT 0,
    error_message   TEXT,
    duration_seconds REAL
);
