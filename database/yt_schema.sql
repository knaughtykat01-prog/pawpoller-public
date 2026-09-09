-- YouTube (yt) Analytics Database Schema — MEDIAPLATS §6 (4.24.0)
--
-- Poll+post video platform. Google OAuth (the operator's own project); the
-- channel's uploads (playlistItems → videos.list) and their counts
-- snapshotted over time. Standard gallery metric shape: views (viewCount) /
-- favorites_count (likeCount) / comments_count (commentCount). The privacy
-- status rides on the row: an unaudited project's uploads stay private, and
-- the row says so. submission_id is the YouTube video id.
--
-- account_id is in the initial schema (fresh platform, no migration needed).
--
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS yt_submissions (
    submission_id   TEXT PRIMARY KEY,
    account_id      INTEGER NOT NULL DEFAULT 0,
    title           TEXT NOT NULL DEFAULT '',
    description     TEXT DEFAULT '',
    tags            TEXT DEFAULT '',
    category_id     TEXT DEFAULT '',
    privacy         TEXT DEFAULT '',
    upload_status   TEXT DEFAULT '',
    username        TEXT NOT NULL DEFAULT '',
    link            TEXT DEFAULT '',
    thumbnail_url   TEXT DEFAULT '',
    duration        TEXT DEFAULT '',
    posted_at       TEXT,
    views           INTEGER DEFAULT 0,
    favorites_count INTEGER DEFAULT 0,
    comments_count  INTEGER DEFAULT 0,
    updated_at      TEXT DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_yt_submissions_account ON yt_submissions(account_id);

CREATE TABLE IF NOT EXISTS yt_snapshots (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id      INTEGER NOT NULL DEFAULT 0,
    submission_id   TEXT NOT NULL,
    polled_at       TEXT NOT NULL DEFAULT (datetime('now')),
    views           INTEGER NOT NULL DEFAULT 0,
    favorites_count INTEGER NOT NULL DEFAULT 0,
    comments_count  INTEGER NOT NULL DEFAULT 0,
    FOREIGN KEY (submission_id) REFERENCES yt_submissions(submission_id)
);

CREATE INDEX IF NOT EXISTS idx_yt_snapshots_submission_polled
    ON yt_snapshots(submission_id, polled_at);
CREATE INDEX IF NOT EXISTS idx_yt_snapshots_polled ON yt_snapshots(polled_at);

CREATE TABLE IF NOT EXISTS yt_poll_log (
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
