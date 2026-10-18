-- Go-live posts — spec 045 (4.81.0). Site-neutral: keyed by the watched site's code
-- ('ttv' today; Picarto can feed it later) and that site's account.
--
--   golive_channels    one row per watched account: on/off, "show me first", cool-down, end action
--   golive_targets     where to announce, each with its own wording
--   golive_streams     every stream seen, keyed by the site's own stream id (one post per stream)
--   golive_runs        at most one per stream: waiting → posting → posted|partial|failed, or skipped|expired|held
--   golive_run_targets one per target per run: what was sent where, and how the end-of-stream edit went
--   golive_signals     webhook message ids already handled (replays ignored), swept after 2 days
--
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS golive_channels (
    site            TEXT NOT NULL,
    account_id      INTEGER NOT NULL,
    enabled         INTEGER NOT NULL DEFAULT 0,
    approve_first   INTEGER NOT NULL DEFAULT 1,
    cooldown_min    INTEGER NOT NULL DEFAULT 15,
    end_action      TEXT NOT NULL DEFAULT 'leave',   -- leave | edit
    live_state      TEXT NOT NULL DEFAULT '',        -- live | offline | '' (not checked yet)
    checked_at      TEXT,
    sub_state       TEXT NOT NULL DEFAULT '',        -- the webhook subscription: '' | ok | revoked | failed
    sub_checked_at  REAL NOT NULL DEFAULT 0,
    PRIMARY KEY (site, account_id)
);

CREATE TABLE IF NOT EXISTS golive_targets (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    site            TEXT NOT NULL,
    account_id      INTEGER NOT NULL,
    platform        TEXT NOT NULL,                   -- a Posts site code, or 'discord' (the announce webhook)
    target_account_id INTEGER NOT NULL DEFAULT 0,
    live_template   TEXT NOT NULL DEFAULT '',
    ended_template  TEXT NOT NULL DEFAULT '',
    allow_mature    INTEGER NOT NULL DEFAULT 0,
    UNIQUE (site, account_id, platform, target_account_id)
);

CREATE TABLE IF NOT EXISTS golive_streams (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    site            TEXT NOT NULL,
    account_id      INTEGER NOT NULL,
    stream_key      TEXT NOT NULL,
    started_at      TEXT,
    ended_at        TEXT,
    title           TEXT NOT NULL DEFAULT '',
    game            TEXT NOT NULL DEFAULT '',
    mature          INTEGER NOT NULL DEFAULT 0,
    link            TEXT NOT NULL DEFAULT '',
    name            TEXT NOT NULL DEFAULT '',
    detected_by     TEXT NOT NULL DEFAULT 'check',   -- signal | check
    state           TEXT NOT NULL DEFAULT 'live',    -- live | ended | final
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (site, account_id, stream_key)
);

CREATE TABLE IF NOT EXISTS golive_runs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    stream_id       INTEGER NOT NULL UNIQUE REFERENCES golive_streams(id),
    state           TEXT NOT NULL,
    nonce           TEXT NOT NULL DEFAULT '',
    reason          TEXT NOT NULL DEFAULT '',
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    decided_at      TEXT,
    decided_by      TEXT NOT NULL DEFAULT '',        -- app | telegram | auto
    tg_message_id   TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS golive_run_targets (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id          INTEGER NOT NULL REFERENCES golive_runs(id),
    platform        TEXT NOT NULL,
    account_id      INTEGER NOT NULL DEFAULT 0,
    post_id         INTEGER,
    status          TEXT NOT NULL DEFAULT 'pending', -- pending | posted | failed | skipped
    external_id     TEXT NOT NULL DEFAULT '',
    external_url    TEXT NOT NULL DEFAULT '',
    error           TEXT NOT NULL DEFAULT '',
    edit_state      TEXT NOT NULL DEFAULT '',        -- '' | edited | cant_edit | edit_failed
    UNIQUE (run_id, platform, account_id)
);

CREATE TABLE IF NOT EXISTS golive_signals (
    message_id      TEXT PRIMARY KEY,
    received_at     REAL NOT NULL
);
