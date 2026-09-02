-- Itaku (IK) Analytics Database Schema
--
-- Itaku provides a public REST API at itaku.ee/api/.
-- No authentication required — only a username is needed.
--
-- Stats tracked: likes (num_likes), comments (num_comments), reshares (num_reshares).
-- Itaku does NOT provide view counts.
-- Content IDs are integers. Content types: image, post.
--
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS ik_submissions (
    submission_id   INTEGER PRIMARY KEY,
    title           TEXT NOT NULL DEFAULT '',
    username        TEXT NOT NULL DEFAULT '',
    posted_at       TEXT,
    content_type    TEXT DEFAULT 'image',
    rating          TEXT DEFAULT '',
    description     TEXT DEFAULT '',
    keywords        TEXT DEFAULT '',
    link            TEXT DEFAULT '',
    thumbnail_url   TEXT DEFAULT '',
    likes           INTEGER DEFAULT 0,
    comments_count  INTEGER DEFAULT 0,
    reshares        INTEGER DEFAULT 0,
    updated_at      TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS ik_snapshots (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    submission_id   INTEGER NOT NULL,
    polled_at       TEXT NOT NULL DEFAULT (datetime('now')),
    likes           INTEGER NOT NULL DEFAULT 0,
    comments_count  INTEGER NOT NULL DEFAULT 0,
    reshares        INTEGER NOT NULL DEFAULT 0,
    FOREIGN KEY (submission_id) REFERENCES ik_submissions(submission_id)
);

CREATE INDEX IF NOT EXISTS idx_ik_snapshots_submission_polled
    ON ik_snapshots(submission_id, polled_at);

CREATE INDEX IF NOT EXISTS idx_ik_snapshots_polled
    ON ik_snapshots(polled_at);

CREATE TABLE IF NOT EXISTS ik_poll_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at      TEXT NOT NULL DEFAULT (datetime('now')),
    finished_at     TEXT,
    status          TEXT NOT NULL DEFAULT 'running',
    submissions_found INTEGER DEFAULT 0,
    snapshots_inserted INTEGER DEFAULT 0,
    error_message   TEXT,
    duration_seconds REAL
);
