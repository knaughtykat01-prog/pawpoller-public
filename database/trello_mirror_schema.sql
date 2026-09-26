-- Trello board mirror (spec 006). Every table is LOC in mirror/registry.py: the
-- mirror belongs to the one instance that owns the Trello connection. A
-- connected desktop has no database and sees the server's copy; a paired desktop
-- must not receive a copy it would then drive (research R1).
--
-- Ids are Trello's own (global, stable), or `tmp_<hex>` for something created in
-- PawPoller that Trello has not confirmed yet. Every mirrored row carries:
--   agreed      JSON: each synced field as both sides last agreed it. The only
--               thing that can tell "changed here" from "we disagree" (R5).
--   removed_at  set when the object vanished from a SUCCESSFUL read — deleted in
--               Trello. The row is kept (FR-025); a failed read never sets it.

CREATE TABLE IF NOT EXISTS trello_boards (
    id            TEXT PRIMARY KEY,
    name          TEXT NOT NULL DEFAULT '',
    closed        INTEGER NOT NULL DEFAULT 0,
    url           TEXT DEFAULT '',
    bg_color      TEXT DEFAULT '',
    bg_image_url  TEXT DEFAULT '',
    is_inbox      INTEGER NOT NULL DEFAULT 0,
    hidden        INTEGER NOT NULL DEFAULT 0,   -- PawPoller-only (FR-004)
    last_activity TEXT DEFAULT '',              -- a skip HINT, never a decision
    comment_cursor TEXT DEFAULT '',             -- newest commentCard action id seen
    webhook_id    TEXT DEFAULT '',
    imported_at   TEXT,                         -- NULL until the first full read committed
    agreed        TEXT NOT NULL DEFAULT '{}',
    removed_at    TEXT,
    synced_at     TEXT
);

CREATE TABLE IF NOT EXISTS trello_lists (
    id         TEXT PRIMARY KEY,
    board_id   TEXT NOT NULL,
    name       TEXT NOT NULL DEFAULT '',
    pos        REAL NOT NULL DEFAULT 0,
    closed     INTEGER NOT NULL DEFAULT 0,
    agreed     TEXT NOT NULL DEFAULT '{}',
    removed_at TEXT,
    synced_at  TEXT
);
CREATE INDEX IF NOT EXISTS idx_trello_lists_board ON trello_lists(board_id, pos);

CREATE TABLE IF NOT EXISTS trello_cards (
    id            TEXT PRIMARY KEY,
    board_id      TEXT NOT NULL,
    list_id       TEXT NOT NULL,
    name          TEXT NOT NULL DEFAULT '',
    desc          TEXT NOT NULL DEFAULT '',
    pos           REAL NOT NULL DEFAULT 0,
    closed        INTEGER NOT NULL DEFAULT 0,
    start         TEXT,
    due           TEXT,
    due_complete  INTEGER NOT NULL DEFAULT 0,
    labels        TEXT NOT NULL DEFAULT '[]',   -- sorted label ids; synced as a SET (R5)
    cover         TEXT NOT NULL DEFAULT '{}',   -- {color, attachment_id, size, brightness}
    url           TEXT DEFAULT '',
    comment_count INTEGER NOT NULL DEFAULT 0,
    last_activity TEXT DEFAULT '',
    agreed        TEXT NOT NULL DEFAULT '{}',
    removed_at    TEXT,
    synced_at     TEXT
);
CREATE INDEX IF NOT EXISTS idx_trello_cards_board ON trello_cards(board_id, list_id, pos);

CREATE TABLE IF NOT EXISTS trello_labels (
    id         TEXT PRIMARY KEY,
    board_id   TEXT NOT NULL,
    name       TEXT NOT NULL DEFAULT '',
    color      TEXT DEFAULT '',
    agreed     TEXT NOT NULL DEFAULT '{}',
    removed_at TEXT,
    synced_at  TEXT
);
CREATE INDEX IF NOT EXISTS idx_trello_labels_board ON trello_labels(board_id);

CREATE TABLE IF NOT EXISTS trello_checklists (
    id         TEXT PRIMARY KEY,
    board_id   TEXT NOT NULL,
    card_id    TEXT NOT NULL,
    name       TEXT NOT NULL DEFAULT '',
    pos        REAL NOT NULL DEFAULT 0,
    agreed     TEXT NOT NULL DEFAULT '{}',
    removed_at TEXT,
    synced_at  TEXT
);
CREATE INDEX IF NOT EXISTS idx_trello_checklists_card ON trello_checklists(card_id);
CREATE INDEX IF NOT EXISTS idx_trello_checklists_board ON trello_checklists(board_id);

CREATE TABLE IF NOT EXISTS trello_check_items (
    id           TEXT PRIMARY KEY,
    board_id     TEXT NOT NULL,
    checklist_id TEXT NOT NULL,
    name         TEXT NOT NULL DEFAULT '',
    pos          REAL NOT NULL DEFAULT 0,
    state        TEXT NOT NULL DEFAULT 'incomplete',
    agreed       TEXT NOT NULL DEFAULT '{}',
    removed_at   TEXT,
    synced_at    TEXT
);
CREATE INDEX IF NOT EXISTS idx_trello_items_checklist ON trello_check_items(checklist_id);
CREATE INDEX IF NOT EXISTS idx_trello_items_board ON trello_check_items(board_id);

-- ⚠ Comment text and author names are the most personal data in the mirror.
-- Never logged, never in a fixture.
CREATE TABLE IF NOT EXISTS trello_comments (
    id          TEXT PRIMARY KEY,               -- the commentCard action id
    board_id    TEXT NOT NULL,
    card_id     TEXT NOT NULL,
    author_id   TEXT DEFAULT '',
    author_name TEXT DEFAULT '',
    text        TEXT NOT NULL DEFAULT '',
    date        TEXT DEFAULT '',
    agreed      TEXT NOT NULL DEFAULT '{}',
    removed_at  TEXT,
    synced_at   TEXT
);
CREATE INDEX IF NOT EXISTS idx_trello_comments_card ON trello_comments(card_id, date);

CREATE TABLE IF NOT EXISTS trello_covers (
    attachment_id TEXT PRIMARY KEY,
    card_id       TEXT NOT NULL,
    url           TEXT DEFAULT '',              -- Trello's download URL (needs header auth)
    file_name     TEXT DEFAULT '',
    mime          TEXT DEFAULT '',
    local_path    TEXT DEFAULT '',              -- relative to DATA_DIR/trello_covers
    fetched_at    TEXT
);

-- Pending local changes, drained in `seq` order by one worker (R4).
CREATE TABLE IF NOT EXISTS trello_outbox (
    seq         INTEGER PRIMARY KEY AUTOINCREMENT,
    op          TEXT NOT NULL,
    object_type TEXT NOT NULL,                  -- board|list|card|label|checklist|checkitem|comment
    object_id   TEXT NOT NULL,
    fields      TEXT NOT NULL DEFAULT '{}',     -- JSON; exactly these advance `agreed` on success
    state       TEXT NOT NULL DEFAULT 'pending',-- pending | held (conflict) | failed (4xx)
    attempts    INTEGER NOT NULL DEFAULT 0,
    next_try_at REAL NOT NULL DEFAULT 0,
    error       TEXT DEFAULT '',                -- operator-facing reason; never content
    created_at  TEXT DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_trello_outbox_state ON trello_outbox(state, seq);

CREATE TABLE IF NOT EXISTS trello_mirror_conflicts (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    object_type  TEXT NOT NULL,
    object_id    TEXT NOT NULL,
    field        TEXT NOT NULL,
    agreed_value TEXT DEFAULT '',
    local_value  TEXT DEFAULT '',
    remote_value TEXT DEFAULT '',
    detected_at  TEXT DEFAULT (datetime('now')),
    UNIQUE (object_type, object_id, field)
);
