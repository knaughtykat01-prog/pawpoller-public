-- PawPoller Database Schema (Inkbunny -- primary platform)
--
-- This is the Inkbunny-specific schema. FurAffinity and Weasyl each have
-- their own schema files (fa_schema.sql, ws_schema.sql) with platform-specific
-- column differences, but all three follow the same core pattern:
--   submissions -> snapshots -> [faving_users/comments]
--
-- WAL mode enables concurrent reads during writes. This is critical because
-- the GUI thread reads data for dashboard display while the background poller
-- thread writes new snapshots. Without WAL, one would block the other.
PRAGMA journal_mode=WAL;
-- Foreign key enforcement must be enabled per-connection in SQLite (it is
-- off by default). This ensures referential integrity across all tables.
PRAGMA foreign_keys=ON;

-- ── submissions ─────────────────────────────────────────────────────────────
-- Stores one row per Inkbunny submission (artwork/story/etc). A row is created
-- the first time a submission is discovered during a gallery poll, and updated
-- on subsequent polls if metadata changes.
--
-- The views, favorites_count, and comments_count columns are DENORMALIZED --
-- they duplicate the latest values from the snapshots table. This avoids an
-- expensive JOIN or subquery every time the UI needs to display current stats
-- (which is on every dashboard refresh). The authoritative time-series data
-- lives in snapshots; these columns are a performance shortcut for "show me
-- current stats for all submissions" queries.
CREATE TABLE IF NOT EXISTS submissions (
    submission_id   INTEGER PRIMARY KEY,    -- Inkbunny's native submission ID
    title           TEXT NOT NULL DEFAULT '',
    username        TEXT NOT NULL DEFAULT '',
    user_id         INTEGER,                -- Inkbunny numeric user ID
    create_datetime TEXT,                   -- When the submission was posted on Inkbunny
    type_name       TEXT DEFAULT '',        -- Inkbunny type: "Picture/Pinup", "Writing", etc.
    rating_id       INTEGER DEFAULT 0,      -- Inkbunny numeric rating (0=General, 1=Mature, 2=Adult)
    rating_name     TEXT DEFAULT '',        -- Human-readable rating label
    thumb_url       TEXT DEFAULT '',        -- Thumbnail image URL
    url             TEXT DEFAULT '',        -- Direct file URL (Inkbunny CDN)
    description     TEXT DEFAULT '',        -- Submission description/body text
    keywords        TEXT DEFAULT '',        -- JSON array of keyword strings (stored as TEXT for SQLite)
    page_count      INTEGER DEFAULT 1,      -- Multi-page submissions (comics, stories)
    -- Denormalized latest stats (see note above). Updated each time a new
    -- snapshot is recorded. These trade storage redundancy for query speed.
    views           INTEGER DEFAULT 0,
    favorites_count INTEGER DEFAULT 0,
    comments_count  INTEGER DEFAULT 0,
    updated_at      TEXT DEFAULT (datetime('now'))  -- Last time this row was modified
);

-- ── snapshots ───────────────────────────────────────────────────────────────
-- Time-series data: one row per (submission, poll cycle). Each poll creates a
-- new snapshot capturing the submission's stats at that moment. This is the
-- authoritative record for graphing stats over time, calculating deltas, and
-- identifying trends.
CREATE TABLE IF NOT EXISTS snapshots (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    submission_id   INTEGER NOT NULL,
    polled_at       TEXT NOT NULL DEFAULT (datetime('now')),  -- When this snapshot was taken
    views           INTEGER NOT NULL DEFAULT 0,
    favorites_count INTEGER NOT NULL DEFAULT 0,
    comments_count  INTEGER NOT NULL DEFAULT 0,
    FOREIGN KEY (submission_id) REFERENCES submissions(submission_id)
);

-- Composite index: efficiently query "all snapshots for submission X ordered by
-- time" (used for time-series charts and delta calculations).
CREATE INDEX IF NOT EXISTS idx_snapshots_submission_polled
    ON snapshots(submission_id, polled_at);

-- Index on polled_at alone: optimizes "all snapshots from the last N hours"
-- queries (used for recent activity summaries and poll log correlation).
CREATE INDEX IF NOT EXISTS idx_snapshots_polled
    ON snapshots(polled_at);

-- ── faving_users ────────────────────────────────────────────────────────────
-- Tracks individual users who favourited each submission. A row is inserted the
-- first time a faving user is detected; it is never updated or deleted. The
-- UNIQUE constraint on (submission_id, user_id) prevents duplicates if the same
-- user appears in multiple poll cycles. This table enables "who favourited this"
-- queries and new-favourite notifications.
-- Note: Only Inkbunny provides per-user favourite data via its API. FurAffinity
-- and Weasyl only expose aggregate favourite counts.
CREATE TABLE IF NOT EXISTS faving_users (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    submission_id   INTEGER NOT NULL,
    user_id         INTEGER NOT NULL,       -- Inkbunny numeric user ID of the faving user
    username        TEXT NOT NULL DEFAULT '',
    first_seen_at   TEXT NOT NULL DEFAULT (datetime('now')),  -- When we first detected this fave
    FOREIGN KEY (submission_id) REFERENCES submissions(submission_id),
    UNIQUE(submission_id, user_id)          -- Prevent duplicate fave records
);

-- Index for "all users who faved submission X" lookups.
CREATE INDEX IF NOT EXISTS idx_faving_users_submission
    ON faving_users(submission_id);

-- Index for "new faves since datetime X" queries (notification system).
CREATE INDEX IF NOT EXISTS idx_faving_users_seen
    ON faving_users(first_seen_at);

-- ── comments ────────────────────────────────────────────────────────────────
-- Stores individual comments on Inkbunny submissions. Each row represents one
-- comment, inserted the first time the comment is seen during a poll. Comments
-- are never updated (Inkbunny does not expose edit history). The is_reply and
-- reply_to_comment_id fields capture comment threading/nesting.
-- Note: This table was added as a migration after the initial schema release
-- (see db.py _run_migrations). It is also defined here so fresh installs get
-- it from the schema file directly.
CREATE TABLE IF NOT EXISTS comments (
    comment_id      INTEGER PRIMARY KEY,    -- Inkbunny's native comment ID
    submission_id   INTEGER NOT NULL,
    username        TEXT NOT NULL DEFAULT '',
    comment_text    TEXT NOT NULL DEFAULT '',
    commented_at    TEXT,            -- Exact timestamp from Inkbunny (e.g. "08 Feb 2026 08:45 AEDT")
    first_seen_at   TEXT NOT NULL DEFAULT (datetime('now')),  -- When our poller first detected it
    is_reply        INTEGER DEFAULT 0,      -- 1 if this is a reply to another comment
    reply_to_comment_id INTEGER,            -- Parent comment ID for threading (NULL if top-level)
    FOREIGN KEY (submission_id) REFERENCES submissions(submission_id)
);

-- Index for "all comments on submission X" lookups.
CREATE INDEX IF NOT EXISTS idx_comments_submission
    ON comments(submission_id);

-- Index for "new comments since datetime X" queries (notification system).
CREATE INDEX IF NOT EXISTS idx_comments_seen
    ON comments(first_seen_at);

-- ── poll_log ────────────────────────────────────────────────────────────────
-- Audit log of every poll cycle execution. One row is created when a poll starts
-- (status='running'), then updated when it finishes with final counts, duration,
-- and success/error status. Used for diagnostics, polling history display, and
-- detecting stalled polls.
CREATE TABLE IF NOT EXISTS poll_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at      TEXT NOT NULL DEFAULT (datetime('now')),
    finished_at     TEXT,                   -- NULL while still running
    status          TEXT NOT NULL DEFAULT 'running',  -- 'running', 'success', or 'error'
    submissions_found INTEGER DEFAULT 0,    -- How many submissions were in the gallery
    snapshots_inserted INTEGER DEFAULT 0,   -- How many new snapshot rows were written
    new_faves_found INTEGER DEFAULT 0,      -- How many newly-detected favourites
    new_comments_found INTEGER DEFAULT 0,  -- How many newly-detected comments
    new_watchers_found INTEGER DEFAULT 0,  -- How many newly-detected watchers
    error_message   TEXT,                   -- Error details if status='error'
    duration_seconds REAL                   -- Wall-clock poll duration
);

-- ── watchers ──────────────────────────────────────────────────────────────
-- Tracks users who watch/follow the authenticated Inkbunny account. Scraped
-- from the web UI since the API has no "who watches me" endpoint. Each row
-- represents one watcher, inserted the first time they are detected. UNIQUE
-- on username prevents duplicates across poll cycles.  user_id is kept for
-- backwards compatibility but defaults to 0 (the usersviewall page does not
-- expose user_id values).
CREATE TABLE IF NOT EXISTS watchers (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id       INTEGER NOT NULL DEFAULT 0,
    username      TEXT NOT NULL,
    first_seen_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(username)
);

CREATE INDEX IF NOT EXISTS idx_watchers_seen ON watchers(first_seen_at);

-- ── session_cache ───────────────────────────────────────────────────────────
-- Singleton table: stores exactly one row (enforced by CHECK(id = 1)) containing
-- the cached Inkbunny session ID (SID) and authenticated username. This avoids
-- re-authenticating on every app restart. The CHECK constraint ensures only one
-- session can exist -- INSERT OR REPLACE with id=1 is used to update it. If the
-- cached session expires, the app re-authenticates and overwrites this row.
CREATE TABLE IF NOT EXISTS session_cache (
    id              INTEGER PRIMARY KEY CHECK (id = 1),  -- Singleton: only id=1 is allowed
    sid             TEXT NOT NULL,           -- Inkbunny session ID (API auth token)
    username        TEXT NOT NULL,           -- Authenticated Inkbunny username
    user_id         INTEGER NOT NULL DEFAULT 0,  -- Inkbunny numeric user ID (for web scraping)
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))  -- When this session was cached
);
