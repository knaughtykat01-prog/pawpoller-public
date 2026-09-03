-- Telegram channel posts and their reaction counts.
--
-- This table is filled DIFFERENTLY from every other platform's, and the
-- difference is the point:
--
--   * Every other <code>_submissions table is populated by POLLING the site —
--     ask it what we published, store what it says.
--   * Telegram has no such endpoint. It does not need one: PawPoller sent every
--     post itself and records each message_id, so the submission list is exact
--     rather than whatever a site chooses to return. This is the one place
--     Telegram is better off than a polled platform.
--
-- The stats are the opposite way round. Reactions arrive ONLY as pushed
-- `message_reaction_count` updates, only while the bot is subscribed via
-- allowed_updates, and there is no query-by-message endpoint and no backfill.
-- So:
--
--   submissions  = complete from day one
--   reactions    = complete only from the day tracking was switched on
--
-- `reactions_from` on the account records that date, so the UI can say "not
-- counted" for older posts instead of showing a 0 that reads as "nobody cared".
-- See docs/specs/telegram_platform.md.

CREATE TABLE IF NOT EXISTS tg_submissions (
    -- "<chat_id>:<message_id>" — a message id is only unique within its chat,
    -- and one install may post to several channels.
    submission_id   TEXT PRIMARY KEY,
    account_id      INTEGER NOT NULL DEFAULT 0,
    chat_id         TEXT NOT NULL DEFAULT '',
    message_id      INTEGER NOT NULL DEFAULT 0,
    title           TEXT DEFAULT '',
    posted_at       TEXT DEFAULT '',
    link            TEXT DEFAULT '',          -- t.me permalink; '' for a private channel
    content_type    TEXT DEFAULT 'artwork',   -- artwork | story | post
    -- Total across every emoji. The per-emoji split lives in reactions_json so
    -- the headline number stays a plain integer the aggregates can sum.
    reactions_count INTEGER DEFAULT 0,
    reactions_json  TEXT DEFAULT '',          -- [{"emoji": "❤", "count": 3}, …]
    -- NULL until the first reaction update arrives for this post. Distinguishes
    -- "no reactions yet" from "we were not listening", which a 0 cannot.
    reactions_at    TEXT,
    updated_at      TEXT DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_tg_sub_account ON tg_submissions(account_id);
CREATE INDEX IF NOT EXISTS idx_tg_sub_chat ON tg_submissions(chat_id, message_id);

CREATE TABLE IF NOT EXISTS tg_snapshots (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id      INTEGER NOT NULL DEFAULT 0,
    submission_id   TEXT NOT NULL,
    polled_at       TEXT DEFAULT (datetime('now')),
    reactions_count INTEGER DEFAULT 0,
    FOREIGN KEY (submission_id) REFERENCES tg_submissions(submission_id)
);

CREATE INDEX IF NOT EXISTS idx_tg_snap_sub ON tg_snapshots(submission_id, polled_at);
CREATE INDEX IF NOT EXISTS idx_tg_snap_polled ON tg_snapshots(polled_at);

-- Poll-cycle log. Telegram's cycle only fetches a subscriber count, so
-- `submissions_found` is always 0 here — the shape is kept identical to every
-- other platform's because /api/platforms/health reads them all through one
-- loop, and a table with a different shape would need a special case in the
-- one place that most benefits from having none.
CREATE TABLE IF NOT EXISTS tg_poll_log (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id         INTEGER NOT NULL DEFAULT 0,
    started_at         TEXT NOT NULL DEFAULT (datetime('now')),
    finished_at        TEXT,
    status             TEXT NOT NULL DEFAULT 'running',
    submissions_found  INTEGER DEFAULT 0,
    snapshots_inserted INTEGER DEFAULT 0,
    error_message      TEXT,
    duration_seconds   REAL
);
