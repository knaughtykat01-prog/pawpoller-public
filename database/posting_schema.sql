-- PawPoller Posting Module Schema
--
-- Adds multi-platform story publishing and update tracking.
-- Three tables:
--   publications   — Registry of what's been posted where (links local story
--                    files to remote platform submission IDs)
--   posting_queue  — Pending/scheduled uploads and updates
--   posting_log    — Immutable audit trail of all posting activity
--
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

-- ============================================================
-- PUBLICATIONS — tracks every story/chapter posted to every platform
-- ============================================================
-- One row per (story, chapter, platform) combination.
-- After uploading Chapter 1 of a story to Inkbunny, this table
-- records the Inkbunny submission_id so we can later edit/update/replace it.

CREATE TABLE IF NOT EXISTS publications (
    pub_id              INTEGER PRIMARY KEY AUTOINCREMENT,

    -- Story identification (maps to local archive)
    story_name          TEXT NOT NULL,
    chapter_index       INTEGER DEFAULT 0,
    chapter_title       TEXT DEFAULT '',

    -- Platform identification
    platform            TEXT NOT NULL,
    external_id         TEXT NOT NULL DEFAULT '',
    external_url        TEXT DEFAULT '',

    -- What was posted (snapshot at time of posting)
    format_file         TEXT DEFAULT '',
    file_hash           TEXT DEFAULT '',
    tags_used           TEXT DEFAULT '[]',
    title_used          TEXT DEFAULT '',
    description_used    TEXT DEFAULT '',
    rating_used         TEXT DEFAULT '',

    -- State tracking
    status              TEXT NOT NULL DEFAULT 'draft',
    first_posted_at     TEXT,
    last_updated_at     TEXT,
    update_count        INTEGER DEFAULT 0,
    last_error          TEXT,

    -- Metadata
    created_at          TEXT NOT NULL DEFAULT (datetime('now')),
    word_count          INTEGER DEFAULT 0,

    UNIQUE(story_name, chapter_index, platform)
);

CREATE INDEX IF NOT EXISTS idx_publications_story
    ON publications(story_name);

CREATE INDEX IF NOT EXISTS idx_publications_platform
    ON publications(platform);

CREATE INDEX IF NOT EXISTS idx_publications_status
    ON publications(status);


-- ============================================================
-- POSTING QUEUE — pending uploads, updates, and scheduled posts
-- ============================================================
-- Items are added by the user (dashboard, Telegram, or API).
-- The posting scheduler picks them up and processes them.

CREATE TABLE IF NOT EXISTS posting_queue (
    queue_id            INTEGER PRIMARY KEY AUTOINCREMENT,

    -- What to post
    story_name          TEXT NOT NULL,
    chapter_index       INTEGER DEFAULT 0,
    platform            TEXT NOT NULL,

    -- Action type
    action              TEXT NOT NULL DEFAULT 'post',

    -- Override fields (NULL = use defaults from tags_upload.txt / split_manifest)
    title_override      TEXT,
    description_override TEXT,
    tags_override       TEXT,
    rating_override     TEXT,
    file_path_override  TEXT,

    -- Scheduling
    scheduled_at        TEXT,
    priority            INTEGER DEFAULT 0,
    requires            TEXT DEFAULT 'any',
    -- Drip campaigns (gap G1): rows created together by one "drip schedule"
    -- share a group id so the campaign can be cancelled as a unit. NULL for
    -- ordinary one-off schedules. (Older DBs gain this via _run_migrations.)
    drip_group          TEXT,

    -- State
    status              TEXT NOT NULL DEFAULT 'pending',
    attempts            INTEGER DEFAULT 0,
    max_attempts        INTEGER DEFAULT 3,
    last_error          TEXT,
    pub_id              INTEGER,

    -- Timestamps
    created_at          TEXT NOT NULL DEFAULT (datetime('now')),
    started_at          TEXT,
    completed_at        TEXT,

    FOREIGN KEY (pub_id) REFERENCES publications(pub_id)
);

CREATE INDEX IF NOT EXISTS idx_posting_queue_status
    ON posting_queue(status, scheduled_at);

CREATE INDEX IF NOT EXISTS idx_posting_queue_story
    ON posting_queue(story_name, platform);


-- ============================================================
-- POSTING LOG — immutable audit trail
-- ============================================================
-- Every posting action (success or failure) gets logged here.
-- Append-only history, unlike posting_queue which tracks current state.

CREATE TABLE IF NOT EXISTS posting_log (
    log_id              INTEGER PRIMARY KEY AUTOINCREMENT,
    pub_id              INTEGER,
    queue_id            INTEGER,
    platform            TEXT NOT NULL,
    story_name          TEXT NOT NULL,
    chapter_index       INTEGER DEFAULT 0,
    action              TEXT NOT NULL,
    status              TEXT NOT NULL,
    external_id         TEXT,
    external_url        TEXT,
    error_message       TEXT,
    duration_seconds    REAL,
    created_at          TEXT NOT NULL DEFAULT (datetime('now')),

    FOREIGN KEY (pub_id) REFERENCES publications(pub_id),
    FOREIGN KEY (queue_id) REFERENCES posting_queue(queue_id)
);

CREATE INDEX IF NOT EXISTS idx_posting_log_pub
    ON posting_log(pub_id);

CREATE INDEX IF NOT EXISTS idx_posting_log_created
    ON posting_log(created_at);
