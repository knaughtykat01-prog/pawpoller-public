-- Picarto (pic) Analytics Database Schema — spec 013 (4.46.0)
--
-- Poll-only livestreaming platform, read from the public API v1 with no login.
-- Picarto's numbers are CHANNEL-level (lifetime views, followers, subscribers), so
-- they live in pic_channel_snapshots, one row per account per poll. Recordings
-- (Picarto "videos", a premium feature) are pic_submissions — inventory, not
-- analytics: Picarto reports 0 views for every recording.
--
-- pic_snapshots keeps the per-submission shape every platform has (the metric
-- registry and the generic link / sparkline / digest code read it by
-- submission_id). Phase 1 writes nothing to it.
--
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS pic_submissions (
    submission_id   TEXT PRIMARY KEY,
    account_id      INTEGER NOT NULL DEFAULT 0,
    title           TEXT NOT NULL DEFAULT '',
    username        TEXT NOT NULL DEFAULT '',
    link            TEXT DEFAULT '',
    thumbnail_url   TEXT DEFAULT '',
    duration_ms     INTEGER DEFAULT 0,
    adult           INTEGER DEFAULT 0,
    posted_at       TEXT,
    views           INTEGER DEFAULT 0,
    updated_at      TEXT DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_pic_submissions_account ON pic_submissions(account_id);

CREATE TABLE IF NOT EXISTS pic_snapshots (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id      INTEGER NOT NULL DEFAULT 0,
    submission_id   TEXT NOT NULL,
    polled_at       TEXT NOT NULL DEFAULT (datetime('now')),
    views           INTEGER NOT NULL DEFAULT 0,
    FOREIGN KEY (submission_id) REFERENCES pic_submissions(submission_id)
);

CREATE INDEX IF NOT EXISTS idx_pic_snapshots_submission_polled
    ON pic_snapshots(submission_id, polled_at);

CREATE TABLE IF NOT EXISTS pic_channel_snapshots (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id      INTEGER NOT NULL DEFAULT 0,
    polled_at       TEXT NOT NULL DEFAULT (datetime('now')),
    channel         TEXT NOT NULL DEFAULT '',
    views           INTEGER NOT NULL DEFAULT 0,
    followers       INTEGER NOT NULL DEFAULT 0,
    subscribers     INTEGER NOT NULL DEFAULT 0,
    online          INTEGER NOT NULL DEFAULT 0,
    viewers         INTEGER NOT NULL DEFAULT 0,
    last_live       TEXT,
    recordings_enabled INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_pic_channel_snapshots_account_polled
    ON pic_channel_snapshots(account_id, polled_at);

CREATE TABLE IF NOT EXISTS pic_poll_log (
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
