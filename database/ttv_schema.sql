-- Twitch (ttv) tracking schema — spec 045 (4.81.0)
--
-- Watched, never posted to. The channel's numbers (live state, viewers, and — when the
-- streamer connects Twitch — followers, subscribers, Bits) are CHANNEL-level and live in
-- ttv_channel_snapshots, one row per account per poll. Past broadcasts and clips are
-- ttv_submissions ('v<id>' / 'c<id>'), each with a view count per poll in ttv_snapshots.
-- A figure Twitch didn't give (no connection) is stored NULL, never a made-up 0.
--
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS ttv_submissions (
    submission_id   TEXT PRIMARY KEY,
    account_id      INTEGER NOT NULL DEFAULT 0,
    kind            TEXT NOT NULL DEFAULT 'vod',     -- vod | clip
    title           TEXT NOT NULL DEFAULT '',
    username        TEXT NOT NULL DEFAULT '',
    link            TEXT DEFAULT '',
    thumbnail_url   TEXT DEFAULT '',
    duration_s      INTEGER DEFAULT 0,
    views           INTEGER DEFAULT 0,
    posted_at       TEXT,
    stream_id       TEXT DEFAULT '',
    updated_at      TEXT DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_ttv_submissions_account ON ttv_submissions(account_id);

CREATE TABLE IF NOT EXISTS ttv_snapshots (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id      INTEGER NOT NULL DEFAULT 0,
    submission_id   TEXT NOT NULL,
    polled_at       TEXT NOT NULL DEFAULT (datetime('now')),
    views           INTEGER NOT NULL DEFAULT 0,
    FOREIGN KEY (submission_id) REFERENCES ttv_submissions(submission_id)
);

CREATE INDEX IF NOT EXISTS idx_ttv_snapshots_submission_polled
    ON ttv_snapshots(submission_id, polled_at);

CREATE TABLE IF NOT EXISTS ttv_channel_snapshots (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id      INTEGER NOT NULL DEFAULT 0,
    polled_at       TEXT NOT NULL DEFAULT (datetime('now')),
    channel         TEXT NOT NULL DEFAULT '',
    live            INTEGER NOT NULL DEFAULT 0,
    viewers         INTEGER NOT NULL DEFAULT 0,
    title           TEXT NOT NULL DEFAULT '',
    game            TEXT NOT NULL DEFAULT '',
    followers       INTEGER,
    subscribers     INTEGER,
    sub_points      INTEGER,
    bits_top        INTEGER,
    connected       INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_ttv_channel_snapshots_account_polled
    ON ttv_channel_snapshots(account_id, polled_at);

CREATE TABLE IF NOT EXISTS ttv_poll_log (
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
