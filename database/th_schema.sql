-- Toyhouse (th) schema — spec 062.
--
-- Poll+post, cookie session, HTML read (no API). The Newgrounds table shape, kept whole so the
-- queries and routes are the same code: Toyhouse has no views, so `views` stays 0, and the
-- Newgrounds-only columns (portal, genre, downloads, votes, score) stay empty. An image has no
-- title; `title` is the names of the characters it hangs under.
-- th_characters holds each character's latest counts (no history in v1).
--
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS th_submissions (
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
    views           INTEGER DEFAULT 0,
    favorites_count INTEGER DEFAULT 0,
    comments_count  INTEGER DEFAULT 0,
    downloads_count INTEGER DEFAULT 0,
    votes           INTEGER DEFAULT 0,
    score           REAL DEFAULT 0,
    updated_at      TEXT DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_th_submissions_account ON th_submissions(account_id);

CREATE TABLE IF NOT EXISTS th_snapshots (
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
    FOREIGN KEY (submission_id) REFERENCES th_submissions(submission_id)
);

CREATE INDEX IF NOT EXISTS idx_th_snapshots_submission_polled
    ON th_snapshots(submission_id, polled_at);
CREATE INDEX IF NOT EXISTS idx_th_snapshots_polled ON th_snapshots(polled_at);

CREATE TABLE IF NOT EXISTS th_poll_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id      INTEGER NOT NULL DEFAULT 0,
    started_at      TEXT NOT NULL DEFAULT (datetime('now')),
    finished_at     TEXT,
    status          TEXT NOT NULL DEFAULT 'running',
    submissions_found INTEGER DEFAULT 0,
    snapshots_inserted INTEGER DEFAULT 0,
    error_message   TEXT,
    duration_seconds REAL,
    full_sweep      INTEGER NOT NULL DEFAULT 0   -- 1 = read every page (weekly); 0 = the newest pages only
);

CREATE TABLE IF NOT EXISTS th_characters (
    character_id    TEXT PRIMARY KEY,
    account_id      INTEGER NOT NULL DEFAULT 0,
    name            TEXT NOT NULL DEFAULT '',
    link            TEXT DEFAULT '',
    favorites_count INTEGER DEFAULT 0,
    images_count    INTEGER DEFAULT 0,
    comments_count  INTEGER DEFAULT 0,
    updated_at      TEXT DEFAULT (datetime('now')),
    read_at         TEXT             -- last time its own page was read (FR-009b's rotation); NULL = never
);

-- One row per successful poll: the account's character totals as known after it (FR-009c). A poll re-reads
-- only some characters, so a per-character history summed per poll would undercount; the totals of the latest
-- known numbers are what the dashboard's chart draws.
CREATE TABLE IF NOT EXISTS th_character_totals (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id          INTEGER NOT NULL DEFAULT 0,
    polled_at           TEXT NOT NULL DEFAULT (datetime('now')),
    characters          INTEGER NOT NULL DEFAULT 0,
    character_favorites INTEGER NOT NULL DEFAULT 0,
    character_comments  INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_th_character_totals ON th_character_totals(account_id, polled_at);
