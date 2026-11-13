-- Supporter numbers — spec 084. Shared by Patreon (pat) and, later, SubscribeStar (ssa).
--
-- Totals only (FR-062, FR-SEC-008): never who a supporter is, their email, or what one person pays. One row per
-- account per COMPLETE poll; a partial member read writes nothing. Classified "Supporters and money"
-- (confidential) in datamap.py, and removed with the account. Every figure is worked out in
-- database/supporter_queries.py and nowhere else.
--
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS supporter_snapshots (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    platform        TEXT NOT NULL,               -- 'pat' (later 'ssa')
    account_id      INTEGER NOT NULL,
    polled_at       TEXT NOT NULL,               -- UTC, "YYYY-MM-DD HH:MM:SS"
    paid_count      INTEGER NOT NULL DEFAULT 0,  -- active, not on a trial; gifted included
    trial_count     INTEGER NOT NULL DEFAULT 0,
    free_count      INTEGER NOT NULL DEFAULT 0,
    total_count     INTEGER,                     -- the site's own total; NULL where it gives none
    monthly_cents   INTEGER NOT NULL DEFAULT 0,  -- Patreon: the campaign's pledge_sum, as Patreon shows it
    currency        TEXT NOT NULL DEFAULT '',
    is_monthly      INTEGER NOT NULL DEFAULT 1,  -- 0 = billed per post
    new_count       INTEGER NOT NULL DEFAULT 0,
    lost_count      INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_supporter_snapshots_account ON supporter_snapshots(account_id, polled_at);

CREATE TABLE IF NOT EXISTS supporter_tier_snapshots (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    snapshot_id     INTEGER NOT NULL REFERENCES supporter_snapshots(id) ON DELETE CASCADE,
    tier_id         TEXT NOT NULL,
    title           TEXT NOT NULL DEFAULT '',
    amount_cents    INTEGER NOT NULL DEFAULT 0,
    patron_count    INTEGER NOT NULL DEFAULT 0,
    monthly_cents   INTEGER NOT NULL DEFAULT 0   -- Σ its active patrons' pledges, before Patreon's adjustments
);

CREATE INDEX IF NOT EXISTS idx_supporter_tier_snapshots ON supporter_tier_snapshots(snapshot_id);
