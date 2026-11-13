-- Patreon (pat) schema — spec 084.
--
-- Poll + post through the creator's signed-in session (Patreon's own web JSON API; no post API exists).
-- The Newgrounds / Toyhouse table shape, kept whole so the shared stats, library and platform-metrics code
-- reads it unchanged: Patreon shows no views, so `views` stays 0 and the Newgrounds-only columns stay empty.
-- `favorites_count` is the post's likes. `audience` is who could see it: public, patrons or tiers.
-- `link` is always built from the post id (https://www.patreon.com/posts/{id}), never Patreon's own url.
--
-- Supporter numbers live in supporter_schema.sql, shared with SubscribeStar later.
--
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS pat_submissions (
    submission_id   TEXT PRIMARY KEY,
    account_id      INTEGER NOT NULL DEFAULT 0,
    portal          TEXT NOT NULL DEFAULT '',
    title           TEXT NOT NULL DEFAULT '',
    description     TEXT DEFAULT '',
    username        TEXT NOT NULL DEFAULT '',
    rating          TEXT DEFAULT '',
    genre           TEXT DEFAULT '',
    link            TEXT DEFAULT '',
    thumbnail_url   TEXT DEFAULT '',
    posted_at       TEXT,
    audience        TEXT NOT NULL DEFAULT '',
    views           INTEGER DEFAULT 0,
    favorites_count INTEGER DEFAULT 0,
    comments_count  INTEGER DEFAULT 0,
    downloads_count INTEGER DEFAULT 0,
    votes           INTEGER DEFAULT 0,
    score           REAL DEFAULT 0,
    updated_at      TEXT DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_pat_submissions_account ON pat_submissions(account_id);

-- One row per post per poll. No views: Patreon shows none.
CREATE TABLE IF NOT EXISTS pat_snapshots (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id      INTEGER NOT NULL DEFAULT 0,
    submission_id   TEXT NOT NULL,
    polled_at       TEXT NOT NULL DEFAULT (datetime('now')),
    views           INTEGER NOT NULL DEFAULT 0,
    favorites_count INTEGER NOT NULL DEFAULT 0,
    comments_count  INTEGER NOT NULL DEFAULT 0,
    score           REAL DEFAULT 0,
    votes           INTEGER DEFAULT 0,
    downloads_count INTEGER DEFAULT 0,
    FOREIGN KEY (submission_id) REFERENCES pat_submissions(submission_id)
);

CREATE INDEX IF NOT EXISTS idx_pat_snapshots_submission_polled
    ON pat_snapshots(submission_id, polled_at);
CREATE INDEX IF NOT EXISTS idx_pat_snapshots_polled ON pat_snapshots(polled_at);

CREATE TABLE IF NOT EXISTS pat_poll_log (
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
