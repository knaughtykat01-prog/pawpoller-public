-- Trello sync for commissions (spec 005). Two tables, both LOC in the mirror —
-- board state belongs to the one instance that claims the board.
--
-- ⚠ Neither table carries a `commission_id`, and that is deliberate.
-- `commissions` is SHR in mirror/registry.py: it syncs between the desktop and
-- the server with key=(client_name, created_at) and exclude=("id",), so the
-- receiving instance assigns its own rowid. A link keyed on `commissions.id`
-- would point at a different commission on the other box, or at nothing, the
-- first time a mirror pull re-created the row. The natural pair costs one
-- composite index and removes the class of bug.

CREATE TABLE IF NOT EXISTS trello_links (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    client_name        TEXT NOT NULL,              -- ┐ the commission's natural key,
    created_at         TEXT NOT NULL,              -- ┘ as mirror/shr.py matches on
    card_id            TEXT NOT NULL,
    card_url           TEXT DEFAULT '',            -- cosmetic; never compared
    board_id           TEXT NOT NULL,
    baseline           TEXT NOT NULL DEFAULT '{}', -- JSON: fields as at the last GOOD sync
    last_synced_at     TEXT DEFAULT '',            -- report only, never an input
    card_last_activity TEXT DEFAULT '',            -- a skip HINT, never a decision
    created_at_row     TEXT DEFAULT (datetime('now')),
    UNIQUE (client_name, created_at),              -- one card per commission
    UNIQUE (card_id)                               -- one commission per card; a
                                                   -- duplicated card is unrecognised,
                                                   -- never a second link
);

CREATE INDEX IF NOT EXISTS idx_trello_links_card ON trello_links(card_id);
CREATE INDEX IF NOT EXISTS idx_trello_links_board ON trello_links(board_id);

CREATE TABLE IF NOT EXISTS trello_conflicts (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    client_name    TEXT NOT NULL,
    created_at     TEXT NOT NULL,
    field          TEXT NOT NULL,
    baseline_value TEXT DEFAULT '',                -- what both sides last agreed
    local_value    TEXT DEFAULT '',                -- what PawPoller now holds
    remote_value   TEXT DEFAULT '',                -- what Trello now holds
    detected_at    TEXT DEFAULT (datetime('now')),
    UNIQUE (client_name, created_at, field)        -- re-detecting updates, never piles up
);

CREATE INDEX IF NOT EXISTS idx_trello_conflicts_key
    ON trello_conflicts(client_name, created_at);
