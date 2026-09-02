-- Archive of Our Own (AO3) Analytics Database Schema
--
-- AO3 runs the OTW Archive open-source software (same as SquidgeWorld).
-- Data is collected by authenticating with a login account and scraping
-- work pages, since there is no public API.
--
-- Stats tracked: hits (views), kudos (favorites), comments, bookmarks.
-- Work IDs are integers. Bookmarks are unique to OTW Archive platforms.
--
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS ao3_submissions (
    submission_id   INTEGER PRIMARY KEY,
    title           TEXT NOT NULL DEFAULT '',
    username        TEXT NOT NULL DEFAULT '',
    posted_at       TEXT,
    fandom          TEXT DEFAULT '',
    rating          TEXT DEFAULT '',
    description     TEXT DEFAULT '',
    keywords        TEXT DEFAULT '',
    link            TEXT DEFAULT '',
    word_count      INTEGER DEFAULT 0,
    chapters        TEXT DEFAULT '1/1',
    views           INTEGER DEFAULT 0,
    favorites_count INTEGER DEFAULT 0,
    comments_count  INTEGER DEFAULT 0,
    bookmarks_count INTEGER DEFAULT 0,
    updated_at      TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS ao3_snapshots (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    submission_id   INTEGER NOT NULL,
    polled_at       TEXT NOT NULL DEFAULT (datetime('now')),
    views           INTEGER NOT NULL DEFAULT 0,
    favorites_count INTEGER NOT NULL DEFAULT 0,
    comments_count  INTEGER NOT NULL DEFAULT 0,
    bookmarks_count INTEGER NOT NULL DEFAULT 0,
    FOREIGN KEY (submission_id) REFERENCES ao3_submissions(submission_id)
);

CREATE INDEX IF NOT EXISTS idx_ao3_snapshots_submission_polled
    ON ao3_snapshots(submission_id, polled_at);

CREATE INDEX IF NOT EXISTS idx_ao3_snapshots_polled
    ON ao3_snapshots(polled_at);

CREATE TABLE IF NOT EXISTS ao3_poll_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at      TEXT NOT NULL DEFAULT (datetime('now')),
    finished_at     TEXT,
    status          TEXT NOT NULL DEFAULT 'running',
    submissions_found INTEGER DEFAULT 0,
    snapshots_inserted INTEGER DEFAULT 0,
    new_kudos_found INTEGER DEFAULT 0,
    error_message   TEXT,
    duration_seconds REAL
);

CREATE TABLE IF NOT EXISTS ao3_kudos_users (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    submission_id   INTEGER NOT NULL,
    username        TEXT NOT NULL DEFAULT '',
    first_seen_at   TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (submission_id) REFERENCES ao3_submissions(submission_id),
    UNIQUE(submission_id, username)
);

CREATE INDEX IF NOT EXISTS idx_ao3_kudos_submission
    ON ao3_kudos_users(submission_id);

CREATE INDEX IF NOT EXISTS idx_ao3_kudos_seen
    ON ao3_kudos_users(first_seen_at);
