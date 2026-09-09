-- SoundCloud (sc) Analytics Database Schema — MEDIAPLATS §3 (4.22.0)
--
-- Poll+post audio platform. OAuth 2.1 + PKCE; the connected user's own tracks
-- (GET /me/tracks) and their counts snapshotted over time. Standard gallery
-- metric shape: views (playback_count) / favorites_count (likes_count) /
-- comments_count (comment_count); reposts and downloads ride on the row only.
-- submission_id is SoundCloud's numeric track id as TEXT; the urn is kept
-- alongside because the numeric id is documented as the older form.
--
-- account_id is in the initial schema (fresh platform, no migration needed).
--
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS sc_submissions (
    submission_id   TEXT PRIMARY KEY,
    account_id      INTEGER NOT NULL DEFAULT 0,
    urn             TEXT DEFAULT '',
    title           TEXT NOT NULL DEFAULT '',
    description     TEXT DEFAULT '',
    genre           TEXT DEFAULT '',
    tag_list        TEXT DEFAULT '',
    sharing         TEXT DEFAULT '',
    username        TEXT NOT NULL DEFAULT '',
    link            TEXT DEFAULT '',
    artwork_url     TEXT DEFAULT '',
    duration_ms     INTEGER DEFAULT 0,
    posted_at       TEXT,
    views           INTEGER DEFAULT 0,
    favorites_count INTEGER DEFAULT 0,
    comments_count  INTEGER DEFAULT 0,
    reposts_count   INTEGER DEFAULT 0,
    downloads_count INTEGER DEFAULT 0,
    updated_at      TEXT DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_sc_submissions_account ON sc_submissions(account_id);

CREATE TABLE IF NOT EXISTS sc_snapshots (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id      INTEGER NOT NULL DEFAULT 0,
    submission_id   TEXT NOT NULL,
    polled_at       TEXT NOT NULL DEFAULT (datetime('now')),
    views           INTEGER NOT NULL DEFAULT 0,
    favorites_count INTEGER NOT NULL DEFAULT 0,
    comments_count  INTEGER NOT NULL DEFAULT 0,
    FOREIGN KEY (submission_id) REFERENCES sc_submissions(submission_id)
);

CREATE INDEX IF NOT EXISTS idx_sc_snapshots_submission_polled
    ON sc_snapshots(submission_id, polled_at);
CREATE INDEX IF NOT EXISTS idx_sc_snapshots_polled ON sc_snapshots(polled_at);

CREATE TABLE IF NOT EXISTS sc_poll_log (
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
