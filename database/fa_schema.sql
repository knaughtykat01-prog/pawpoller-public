-- FurAffinity Analytics Database Schema
--
-- Differences from the Inkbunny (IB) schema (schema.sql):
--
-- SUBMISSION METADATA:
--   - category/theme/species/gender: FA has rich content classification fields
--     that IB does not. FA submissions are tagged with structured metadata
--     like category ("Artwork > Digital"), theme ("General Furry Art"),
--     species ("Fox"), and gender ("Male"). IB relies solely on free-form
--     keywords for this information.
--   - download_url instead of url: FA calls the full-resolution file link
--     "download_url" (it's behind a download button on the page). IB uses
--     "url" for the equivalent field. Weasyl uses "media_url".
--   - thumbnail_url instead of thumb_url: different naming convention from IB.
--   - posted_at instead of create_datetime: FA uses a different timestamp
--     field name for when the submission was originally posted.
--   - link: FA includes a direct web URL to the submission page. IB constructs
--     these from the submission ID instead.
--   - No user_id: FA scraping does not reliably expose numeric user IDs.
--   - No page_count: FA does not have multi-page submissions like IB.
--   - No rating_id: FA uses a text rating ("General"/"Mature"/"Adult") rather
--     than IB's numeric rating_id + rating_name pair.
--   - No type_name: FA uses category/theme instead of IB's type_name
--     ("Picture/Pinup", "Writing", etc).
--
-- COMMENTS:
--   - comment_id is TEXT (not INTEGER): FA comment IDs are extracted from HTML
--     anchors and may not be pure integers.
--   - reply_to (TEXT) + reply_level (INTEGER) instead of IB's is_reply +
--     reply_to_comment_id: FA threading uses indentation levels rather than
--     simple parent references.
--   - is_deleted flag: FA comments can be deleted by moderators; the row is
--     kept but flagged. IB does not expose this information.
--
-- FAVING USERS:
--   - No fa_faving_users table: FA does not expose per-user favourite data
--     through scraping. Only aggregate favourite counts are available.
--
-- POLL LOG:
--   - new_comments_found instead of new_faves_found: FA polls track new
--     comments (since individual faves are not available) rather than faves.
--
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

-- ── fa_submissions ──────────────────────────────────────────────────────────
-- One row per FurAffinity submission. Created when first discovered during a
-- gallery poll. Updated on subsequent polls with latest metadata and stats.
-- See header comments for field differences from the IB schema.
CREATE TABLE IF NOT EXISTS fa_submissions (
    submission_id   INTEGER PRIMARY KEY,    -- FA's native submission ID
    title           TEXT NOT NULL DEFAULT '',
    username        TEXT NOT NULL DEFAULT '',
    posted_at       TEXT,                   -- When posted on FA (scraped from page)
    -- FA-specific structured classification fields (IB uses only free-form keywords):
    category        TEXT DEFAULT '',        -- e.g. "Artwork (Digital)" or "Story"
    theme           TEXT DEFAULT '',        -- e.g. "General Furry Art", "Adult"
    species         TEXT DEFAULT '',        -- e.g. "Fox", "Wolf", "Unspecified"
    gender          TEXT DEFAULT '',        -- e.g. "Male", "Female", "Any"
    rating          TEXT DEFAULT '',        -- "General", "Mature", or "Adult" (text, not numeric like IB)
    thumbnail_url   TEXT DEFAULT '',        -- Preview image URL
    download_url    TEXT DEFAULT '',        -- Full-resolution file URL (FA's "Download" link)
    description     TEXT DEFAULT '',        -- Submission description HTML
    keywords        TEXT DEFAULT '',        -- JSON array of tag strings (same format as IB)
    link            TEXT DEFAULT '',        -- Direct URL to the FA submission page
    -- Denormalized latest stats (same pattern as IB -- see schema.sql for rationale).
    views           INTEGER DEFAULT 0,
    favorites_count INTEGER DEFAULT 0,
    comments_count  INTEGER DEFAULT 0,
    updated_at      TEXT DEFAULT (datetime('now'))
);

-- ── fa_snapshots ────────────────────────────────────────────────────────────
-- Time-series stats, same pattern as IB snapshots. One row per poll cycle
-- per submission.
CREATE TABLE IF NOT EXISTS fa_snapshots (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    submission_id   INTEGER NOT NULL,
    polled_at       TEXT NOT NULL DEFAULT (datetime('now')),
    views           INTEGER NOT NULL DEFAULT 0,
    favorites_count INTEGER NOT NULL DEFAULT 0,
    comments_count  INTEGER NOT NULL DEFAULT 0,
    FOREIGN KEY (submission_id) REFERENCES fa_submissions(submission_id)
);

-- Composite index for time-series queries on a specific submission.
CREATE INDEX IF NOT EXISTS idx_fa_snapshots_submission_polled
    ON fa_snapshots(submission_id, polled_at);

-- Index for "all snapshots in a time range" queries.
CREATE INDEX IF NOT EXISTS idx_fa_snapshots_polled
    ON fa_snapshots(polled_at);

-- ── fa_comments ─────────────────────────────────────────────────────────────
-- Individual comments on FA submissions. Unlike IB, FA comment_id is TEXT
-- (extracted from HTML anchors), threading uses reply_to + reply_level
-- (indentation depth), and deleted comments are tracked via is_deleted flag.
CREATE TABLE IF NOT EXISTS fa_comments (
    comment_id      TEXT PRIMARY KEY,       -- TEXT not INTEGER: FA IDs from HTML anchors
    submission_id   INTEGER NOT NULL,
    username        TEXT NOT NULL DEFAULT '',
    comment_text    TEXT NOT NULL DEFAULT '',
    commented_at    TEXT,                   -- Timestamp scraped from the FA page
    first_seen_at   TEXT NOT NULL DEFAULT (datetime('now')),
    reply_to        TEXT,                   -- Parent comment_id (TEXT to match PK type)
    reply_level     INTEGER DEFAULT 0,      -- Nesting depth (0=top-level, 1=reply, 2=reply-to-reply...)
    is_deleted      INTEGER DEFAULT 0,      -- 1 if comment was removed by moderator/user
    FOREIGN KEY (submission_id) REFERENCES fa_submissions(submission_id)
);

CREATE INDEX IF NOT EXISTS idx_fa_comments_submission
    ON fa_comments(submission_id);

CREATE INDEX IF NOT EXISTS idx_fa_comments_seen
    ON fa_comments(first_seen_at);

-- ── fa_poll_log ─────────────────────────────────────────────────────────────
-- Audit log for FA poll cycles. Tracks new_comments_found instead of IB's
-- new_faves_found because FA does not expose individual favourite user data.
CREATE TABLE IF NOT EXISTS fa_poll_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at      TEXT NOT NULL DEFAULT (datetime('now')),
    finished_at     TEXT,
    status          TEXT NOT NULL DEFAULT 'running',
    submissions_found INTEGER DEFAULT 0,
    snapshots_inserted INTEGER DEFAULT 0,
    new_comments_found INTEGER DEFAULT 0,   -- FA tracks comments, not faves (no per-user fave data)
    new_watchers_found INTEGER DEFAULT 0,  -- How many newly-detected FA watchers
    error_message   TEXT,
    duration_seconds REAL
);

-- ── fa_watchers ───────────────────────────────────────────────────────────
-- Tracks users watching the authenticated FurAffinity account. Fetched via
-- FAExport /user/{name}/watchers.json. Only usernames are available (no
-- user_ids from FAExport). UNIQUE on username prevents duplicates.
--
-- Spam protection columns:
--   confirmed    -- 0 = pending (seen once), 1 = confirmed (seen in 2+ consecutive polls)
--   last_seen_at -- when the watcher was last seen in FAExport's list
--   is_spam      -- 1 if flagged as bot/spam by heuristics or profile sniff
--   notified     -- 1 if a notification has already been sent for this watcher
CREATE TABLE IF NOT EXISTS fa_watchers (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    username      TEXT NOT NULL,
    first_seen_at TEXT NOT NULL DEFAULT (datetime('now')),
    confirmed     INTEGER NOT NULL DEFAULT 0,
    last_seen_at  TEXT DEFAULT (datetime('now')),
    is_spam       INTEGER NOT NULL DEFAULT 0,
    notified      INTEGER NOT NULL DEFAULT 0,
    UNIQUE(username)
);

CREATE INDEX IF NOT EXISTS idx_fa_watchers_seen ON fa_watchers(first_seen_at);
-- NOTE: idx_fa_watchers_pending is created in db.py _run_migrations() because
-- existing databases may not have the confirmed/notified columns yet when this
-- schema file runs (the columns are added by migration).

-- ── fa_profile_stats ──────────────────────────────────────────────────────
-- Time-series tracking of the authenticated user's FA profile pageviews.
-- FAExport's /user/{name}.json returns a top-level "pageviews" field that
-- represents how many times the user's profile page has been visited.
-- One row per poll cycle captures this value for historical charting.
CREATE TABLE IF NOT EXISTS fa_profile_stats (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    polled_at   TEXT NOT NULL DEFAULT (datetime('now')),
    pageviews   INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_fa_profile_stats_polled
    ON fa_profile_stats(polled_at);
