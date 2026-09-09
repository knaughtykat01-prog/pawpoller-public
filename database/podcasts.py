"""Podcast feeds and their episodes — MEDIAPLATS §4 (4.21.1).

A feed is an RSS document PawPoller serves itself at a public URL; an episode is one of the
Library's audio pieces listed in it. Directories (Apple Podcasts, Spotify, Amazon Music,
Pocket Casts) fetch the feed on their own schedule, so publishing an episode is a row insert
and nothing is uploaded anywhere. The piece is never copied or changed — the feed points at
the artwork archive's own file through a per-feed public path.

Both tables are **shared** in the mirror (SHR): a feed is hosted by the server, the desktop
edits it through the paired connection, and the rows are the truth on both boxes.

Schema lives here (guarded ``CREATE TABLE IF NOT EXISTS``) and is applied both by
``db._run_migrations`` and by ``ensure()`` from the routes — the promos pattern (4.16.0) —
so a fresh test database and a years-old install both have it. Indexes are created AFTER the
tables, here (the migration-order gotcha).
"""
from __future__ import annotations

import re
import sqlite3
import uuid

FEEDS_SQL = """
    CREATE TABLE IF NOT EXISTS podcast_feeds (
        feed_id      INTEGER PRIMARY KEY AUTOINCREMENT,
        slug         TEXT NOT NULL UNIQUE,
        title        TEXT NOT NULL DEFAULT '',
        description  TEXT NOT NULL DEFAULT '',
        author       TEXT NOT NULL DEFAULT '',
        owner_email  TEXT NOT NULL DEFAULT '',
        language     TEXT NOT NULL DEFAULT 'en',
        category     TEXT NOT NULL DEFAULT 'Arts',
        explicit     INTEGER NOT NULL DEFAULT 0,
        artwork_name TEXT,
        artwork_file TEXT,
        link         TEXT NOT NULL DEFAULT '',
        created_at   TEXT NOT NULL DEFAULT (datetime('now')),
        updated_at   TEXT NOT NULL DEFAULT (datetime('now'))
    )
"""
EPISODES_SQL = """
    CREATE TABLE IF NOT EXISTS podcast_episodes (
        episode_id     INTEGER PRIMARY KEY AUTOINCREMENT,
        feed_id        INTEGER NOT NULL,
        artwork_name   TEXT NOT NULL,
        guid           TEXT NOT NULL UNIQUE,
        title          TEXT NOT NULL DEFAULT '',
        notes          TEXT NOT NULL DEFAULT '',
        explicit       INTEGER NOT NULL DEFAULT 0,
        season         INTEGER,
        episode_number INTEGER,
        published_at   TEXT NOT NULL DEFAULT (datetime('now')),
        created_at     TEXT NOT NULL DEFAULT (datetime('now')),
        updated_at     TEXT NOT NULL DEFAULT (datetime('now'))
    )
"""
INDEX_SQL = (
    "CREATE INDEX IF NOT EXISTS idx_podcast_episodes_feed ON podcast_episodes(feed_id, published_at)",
    "CREATE INDEX IF NOT EXISTS idx_podcast_episodes_piece ON podcast_episodes(artwork_name)",
)

_FEED_COLS = ("feed_id", "slug", "title", "description", "author", "owner_email", "language",
              "category", "explicit", "artwork_name", "artwork_file", "link", "created_at", "updated_at")
_EP_COLS = ("episode_id", "feed_id", "artwork_name", "guid", "title", "notes", "explicit", "season",
            "episode_number", "published_at", "created_at", "updated_at")

_SLUG_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?$")


def ensure(conn: sqlite3.Connection) -> None:
    conn.execute(FEEDS_SQL)
    conn.execute(EPISODES_SQL)
    for sql in INDEX_SQL:
        conn.execute(sql)


def slugify(text: str) -> str:
    """A feed slug: lowercase letters, digits and single hyphens, ≤ 64 chars."""
    s = re.sub(r"[^a-z0-9]+", "-", (text or "").strip().lower()).strip("-")
    return s[:64].strip("-") or "feed"


def valid_slug(slug: str) -> bool:
    return bool(_SLUG_RE.match(slug or ""))


def _feed(r) -> dict | None:
    if not r:
        return None
    d = dict(zip(_FEED_COLS, r))
    d["explicit"] = bool(d["explicit"])
    return d


def _ep(r) -> dict | None:
    if not r:
        return None
    d = dict(zip(_EP_COLS, r))
    d["explicit"] = bool(d["explicit"])
    return d


# ── feeds ────────────────────────────────────────────────────────────────────

def list_feeds(conn: sqlite3.Connection) -> list[dict]:
    ensure(conn)
    return [_feed(r) for r in conn.execute(
        "SELECT " + ", ".join(_FEED_COLS) + " FROM podcast_feeds ORDER BY title, feed_id").fetchall()]


def get_feed(conn: sqlite3.Connection, feed_id: int) -> dict | None:
    ensure(conn)
    return _feed(conn.execute("SELECT " + ", ".join(_FEED_COLS) + " FROM podcast_feeds WHERE feed_id = ?",
                              (int(feed_id),)).fetchone())


def get_feed_by_slug(conn: sqlite3.Connection, slug: str) -> dict | None:
    ensure(conn)
    return _feed(conn.execute("SELECT " + ", ".join(_FEED_COLS) + " FROM podcast_feeds WHERE slug = ?",
                              (slug or "",)).fetchone())


def create_feed(conn: sqlite3.Connection, *, slug: str, title: str, description: str = "", author: str = "",
                owner_email: str = "", language: str = "en", category: str = "Arts", explicit: bool = False,
                artwork_name: str | None = None, artwork_file: str | None = None, link: str = "") -> int:
    ensure(conn)
    if not valid_slug(slug):
        raise ValueError("A feed address is lowercase letters, digits and hyphens (up to 64)")
    if not (title or "").strip():
        raise ValueError("A feed needs a title")
    cur = conn.execute(
        "INSERT INTO podcast_feeds (slug, title, description, author, owner_email, language, category, explicit, "
        "artwork_name, artwork_file, link) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (slug, title.strip(), description or "", author or "", owner_email or "", language or "en",
         category or "Arts", 1 if explicit else 0, artwork_name or None, artwork_file or None, link or ""))
    conn.commit()
    return int(cur.lastrowid)


_FEED_EDITABLE = ("title", "description", "author", "owner_email", "language", "category", "explicit",
                  "artwork_name", "artwork_file", "link")


def update_feed(conn: sqlite3.Connection, feed_id: int, **fields) -> None:
    """Partial update; the slug is not editable (directories keep the URL)."""
    ensure(conn)
    sets, args = [], []
    for k in _FEED_EDITABLE:
        if k in fields:
            v = fields[k]
            if k == "explicit":
                v = 1 if v else 0
            sets.append(f"{k} = ?")
            args.append(v)
    if not sets:
        return
    sets.append("updated_at = datetime('now')")
    args.append(int(feed_id))
    conn.execute("UPDATE podcast_feeds SET " + ", ".join(sets) + " WHERE feed_id = ?", args)
    conn.commit()


def delete_feed(conn: sqlite3.Connection, feed_id: int) -> None:
    """Removes the feed and its episode rows. The pieces are untouched (never delete artwork)."""
    ensure(conn)
    conn.execute("DELETE FROM podcast_episodes WHERE feed_id = ?", (int(feed_id),))
    conn.execute("DELETE FROM podcast_feeds WHERE feed_id = ?", (int(feed_id),))
    conn.commit()


# ── episodes ─────────────────────────────────────────────────────────────────

def list_episodes(conn: sqlite3.Connection, feed_id: int) -> list[dict]:
    """Newest first — the order a feed lists them."""
    ensure(conn)
    return [_ep(r) for r in conn.execute(
        "SELECT " + ", ".join(_EP_COLS) + " FROM podcast_episodes WHERE feed_id = ? "
        "ORDER BY published_at DESC, episode_id DESC", (int(feed_id),)).fetchall()]


def episodes_for_piece(conn: sqlite3.Connection, artwork_name: str) -> list[dict]:
    """Every feed a piece is an episode of (the board's Episodes line)."""
    ensure(conn)
    rows = conn.execute(
        "SELECT e.episode_id, e.feed_id, e.guid, e.title, e.published_at, f.slug, f.title "
        "FROM podcast_episodes e JOIN podcast_feeds f ON f.feed_id = e.feed_id "
        "WHERE e.artwork_name = ? ORDER BY e.published_at DESC", (artwork_name,)).fetchall()
    return [{"episode_id": r[0], "feed_id": r[1], "guid": r[2], "title": r[3], "published_at": r[4],
             "feed_slug": r[5], "feed_title": r[6]} for r in rows]


def get_episode(conn: sqlite3.Connection, episode_id: int) -> dict | None:
    ensure(conn)
    return _ep(conn.execute("SELECT " + ", ".join(_EP_COLS) + " FROM podcast_episodes WHERE episode_id = ?",
                            (int(episode_id),)).fetchone())


def get_episode_by_guid(conn: sqlite3.Connection, feed_id: int, guid: str) -> dict | None:
    ensure(conn)
    return _ep(conn.execute("SELECT " + ", ".join(_EP_COLS) + " FROM podcast_episodes WHERE feed_id = ? AND guid = ?",
                            (int(feed_id), guid or "")).fetchone())


def find_episode(conn: sqlite3.Connection, feed_id: int, artwork_name: str) -> dict | None:
    ensure(conn)
    return _ep(conn.execute("SELECT " + ", ".join(_EP_COLS) + " FROM podcast_episodes WHERE feed_id = ? AND artwork_name = ?",
                            (int(feed_id), artwork_name)).fetchone())


def add_episode(conn: sqlite3.Connection, *, feed_id: int, artwork_name: str, title: str, notes: str = "",
                explicit: bool = False, season: int | None = None, episode_number: int | None = None,
                published_at: str | None = None) -> dict:
    """Publish a piece as an episode. The GUID is minted once and never changes — directories
    key on it, so re-adding the same piece to the same feed returns the existing row."""
    ensure(conn)
    existing = find_episode(conn, feed_id, artwork_name)
    if existing:
        return existing
    guid = uuid.uuid4().hex
    conn.execute(
        "INSERT INTO podcast_episodes (feed_id, artwork_name, guid, title, notes, explicit, season, episode_number, published_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, COALESCE(?, datetime('now')))",
        (int(feed_id), artwork_name, guid, (title or "").strip() or artwork_name, notes or "", 1 if explicit else 0,
         season, episode_number, published_at))
    conn.commit()
    return get_episode_by_guid(conn, feed_id, guid)


_EP_EDITABLE = ("title", "notes", "explicit", "season", "episode_number", "published_at")


def update_episode(conn: sqlite3.Connection, episode_id: int, **fields) -> None:
    ensure(conn)
    sets, args = [], []
    for k in _EP_EDITABLE:
        if k in fields:
            v = fields[k]
            if k == "explicit":
                v = 1 if v else 0
            sets.append(f"{k} = ?")
            args.append(v)
    if not sets:
        return
    sets.append("updated_at = datetime('now')")
    args.append(int(episode_id))
    conn.execute("UPDATE podcast_episodes SET " + ", ".join(sets) + " WHERE episode_id = ?", args)
    conn.commit()


def remove_episode(conn: sqlite3.Connection, episode_id: int) -> None:
    """Unpublish: the row goes, the piece stays."""
    ensure(conn)
    conn.execute("DELETE FROM podcast_episodes WHERE episode_id = ?", (int(episode_id),))
    conn.commit()
