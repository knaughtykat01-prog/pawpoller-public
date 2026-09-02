-- Collections — a user-curated master container for one "piece" across every
-- place it lives: gallery works (FA/IB/Itaku…), microblog submissions (X/Bsky…),
-- and an optional companion story. Members are POLYMORPHIC references resolved
-- live at read time, so pooled analytics/tags/locations stay current.
-- See docs/specs/collections.md.

CREATE TABLE IF NOT EXISTS collections (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    name          TEXT NOT NULL DEFAULT '',
    cover_kind    TEXT DEFAULT '',        -- 'artwork' | 'story' | 'url' | ''
    cover_ref     TEXT DEFAULT '',        -- artwork/story name, or an image URL
    notes         TEXT DEFAULT '',
    created_at    TEXT DEFAULT (datetime('now')),
    updated_at    TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS collection_members (
    collection_id INTEGER NOT NULL,
    member_type   TEXT NOT NULL,          -- 'work' | 'submission' | 'post'
    member_ref    TEXT NOT NULL,          -- work: 'artwork:Name' / 'story:Name'
                                          -- submission: 'fa:12345' (platform:submission_id)
                                          -- post: '<post_id>'
    role          TEXT DEFAULT '',        -- 'primary' | 'art' | 'story' | 'announcement' | ''
    added_at      TEXT DEFAULT (datetime('now')),
    PRIMARY KEY (collection_id, member_type, member_ref),
    FOREIGN KEY (collection_id) REFERENCES collections(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_collection_members_cid
    ON collection_members(collection_id);
