-- Posts module (microblog / "tweet-like" publishing) — 2.49.0
--
-- A self-contained store for short-form posts composed IN PawPoller and pushed
-- to microblog platforms (Bluesky, Mastodon, and — later — Threads/Tumblr/X).
-- Deliberately NOT the story/artwork `publications` registry: a post has no
-- title/chapters/file, so forcing it onto the Package/publications model buys
-- nothing. Analytics for what these posts *earn* still flows through the normal
-- per-platform pollers (bsky_submissions etc.); this pair only tracks the
-- compose→publish side.

CREATE TABLE IF NOT EXISTS posts (
    post_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    body         TEXT NOT NULL DEFAULT '',        -- the post text
    rating       TEXT NOT NULL DEFAULT 'general', -- general | mature | adult
    image_path   TEXT NOT NULL DEFAULT '',        -- optional local media (DATA_DIR/posts_media/…)
    image_alt    TEXT NOT NULL DEFAULT '',        -- alt text for the image
    -- Threads (gap-wave-3 §4 / G8): a part row points at its parent post; 0 =
    -- a top-level post. Parts are full post rows so post_media/post_publications
    -- work unchanged. Older DBs gain these via _run_migrations.
    parent_post_id INTEGER NOT NULL DEFAULT 0,
    thread_ordinal INTEGER NOT NULL DEFAULT 0,
    created_at   TEXT NOT NULL DEFAULT '',
    updated_at   TEXT NOT NULL DEFAULT ''
);

-- One row per attached image (2.58.0). Posts can carry up to 4 images (the
-- X/Bluesky/Mastodon cap). The legacy posts.image_path/image_alt columns are
-- kept and still hold the FIRST image, so anything that reads them (feed
-- thumbnail, /image?post_id=) keeps working; this table holds the full ordered
-- set that the publisher fans out per platform.
CREATE TABLE IF NOT EXISTS post_media (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    post_id  INTEGER NOT NULL,
    ordinal  INTEGER NOT NULL DEFAULT 0,      -- display / upload order (0-based)
    path     TEXT NOT NULL DEFAULT '',        -- DATA_DIR/posts_media/…
    alt      TEXT NOT NULL DEFAULT ''         -- per-image alt text
);
CREATE INDEX IF NOT EXISTS idx_post_media_post ON post_media(post_id);

-- One row per (post, platform, account) publish attempt.
CREATE TABLE IF NOT EXISTS post_publications (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    post_id      INTEGER NOT NULL,
    platform     TEXT NOT NULL,
    account_id   INTEGER NOT NULL DEFAULT 0,
    status       TEXT NOT NULL DEFAULT 'pending', -- pending | posted | failed
    external_id  TEXT NOT NULL DEFAULT '',        -- platform post id / URI
    external_url TEXT NOT NULL DEFAULT '',         -- canonical link to the live post
    error        TEXT NOT NULL DEFAULT '',
    created_at   TEXT NOT NULL DEFAULT '',
    UNIQUE(post_id, platform, account_id)
);

CREATE INDEX IF NOT EXISTS idx_post_pub_post ON post_publications(post_id);

-- Handle-book (2.61.0): a person you tag, with their per-platform @handle. The
-- same piece is published once but each network needs that person's OWN handle
-- (@name.bsky.social vs @xname vs @user@instance vs @threadsname), so a post
-- stores an alias token and the publisher expands it per platform at send time.
CREATE TABLE IF NOT EXISTS post_contacts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL DEFAULT '',   -- display name / the @alias you type
    handle_bsky TEXT NOT NULL DEFAULT '',    -- e.g. name.bsky.social (no leading @)
    handle_tw   TEXT NOT NULL DEFAULT '',    -- e.g. xname
    handle_mast TEXT NOT NULL DEFAULT '',    -- e.g. user@instance.social
    handle_thr  TEXT NOT NULL DEFAULT '',    -- e.g. threadsname
    handle_tum  TEXT NOT NULL DEFAULT '',    -- e.g. blogname
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

-- One row per (post, alias token) → the contact it's bound to. The publisher
-- reads these to expand @alias into each platform's handle (+ build Bluesky
-- mention facets). Unbound @tokens simply have no row and stay plain text.
CREATE TABLE IF NOT EXISTS post_mentions (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    post_id    INTEGER NOT NULL,
    token      TEXT NOT NULL DEFAULT '',    -- the @alias as typed, without the @
    contact_id INTEGER NOT NULL DEFAULT 0,
    UNIQUE(post_id, token)
);
CREATE INDEX IF NOT EXISTS idx_post_mentions_post ON post_mentions(post_id);
