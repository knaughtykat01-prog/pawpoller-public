-- Facebook Pages (fb) stats — spec 029 (4.59.0)
--
-- One row per post on each connected Page (posted through PawPoller or on Facebook itself).
-- submission_id is the POST id ``<page_id>_<post_id>``; a video post also keeps its video id,
-- because a video published through PawPoller is recorded by its video id (see
-- fb_queries.rekey_video_member). Numbers are NULL when Facebook gave none ("not available"),
-- never a made-up 0. Rows are never deleted: a post gone from Facebook is flagged ``deleted``.
--
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS fb_submissions (
    submission_id     TEXT PRIMARY KEY,
    account_id        INTEGER NOT NULL DEFAULT 0,
    page_id           TEXT NOT NULL DEFAULT '',
    video_id          TEXT NOT NULL DEFAULT '',
    title             TEXT NOT NULL DEFAULT '',
    full_text         TEXT DEFAULT '',
    link              TEXT DEFAULT '',
    posted_at         TEXT,
    content_type      TEXT NOT NULL DEFAULT 'post',
    made_by_pawpoller INTEGER NOT NULL DEFAULT 0,
    views             INTEGER,
    reactions         INTEGER,
    reactions_json    TEXT NOT NULL DEFAULT '{}',
    comments          INTEGER,
    shares            INTEGER,
    plays             INTEGER,
    deleted           INTEGER NOT NULL DEFAULT 0,
    updated_at        TEXT DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_fb_submissions_account ON fb_submissions(account_id);
CREATE INDEX IF NOT EXISTS idx_fb_submissions_video ON fb_submissions(video_id);

CREATE TABLE IF NOT EXISTS fb_snapshots (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id      INTEGER NOT NULL DEFAULT 0,
    submission_id   TEXT NOT NULL,
    polled_at       TEXT NOT NULL DEFAULT (datetime('now')),
    views           INTEGER,
    reactions       INTEGER,
    comments        INTEGER,
    shares          INTEGER,
    plays           INTEGER,
    FOREIGN KEY (submission_id) REFERENCES fb_submissions(submission_id)
);

CREATE INDEX IF NOT EXISTS idx_fb_snapshots_submission_polled ON fb_snapshots(submission_id, polled_at);

CREATE TABLE IF NOT EXISTS fb_poll_log (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id         INTEGER NOT NULL DEFAULT 0,
    started_at         TEXT NOT NULL DEFAULT (datetime('now')),
    finished_at        TEXT,
    status             TEXT NOT NULL DEFAULT 'running',
    submissions_found  INTEGER DEFAULT 0,
    snapshots_inserted INTEGER DEFAULT 0,
    new_comments       INTEGER DEFAULT 0,
    missing_permission TEXT NOT NULL DEFAULT '',
    error_message      TEXT,
    duration_seconds   REAL
);
