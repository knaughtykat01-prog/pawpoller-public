"""Data-access helpers for the Posts (microblog) module — 2.49.0.

Thin CRUD over the ``posts`` + ``post_publications`` tables. No business logic
lives here (that's ``posting/post_publisher.py``); these just read/write rows
and hand back plain dicts. Timestamps are supplied by the caller so the pure
helpers stay side-effect free and testable.
"""
from __future__ import annotations

import sqlite3


# ── posts ──────────────────────────────────────────────────────────

def create_post(conn: sqlite3.Connection, *, body: str, rating: str = "general",
                image_path: str = "", image_alt: str = "", now: str = "",
                parent_post_id: int = 0, thread_ordinal: int = 0) -> int:
    """Insert a draft post and return its post_id. parent_post_id/thread_ordinal
    make the row a thread PART (gap-wave-3 §4); 0 = a top-level post."""
    cur = conn.execute(
        "INSERT INTO posts (body, rating, image_path, image_alt, created_at, updated_at,"
        " parent_post_id, thread_ordinal) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (body, rating, image_path, image_alt, now, now,
         int(parent_post_id or 0), int(thread_ordinal or 0)),
    )
    conn.commit()
    return int(cur.lastrowid)


def update_post(conn: sqlite3.Connection, post_id: int, *, now: str = "", **fields) -> None:
    """Patch a post's editable columns (body/rating/image_path/image_alt)."""
    allowed = {"body", "rating", "image_path", "image_alt"}
    sets, vals = [], []
    for k, v in fields.items():
        if k in allowed:
            sets.append(f"{k} = ?")
            vals.append(v)
    if not sets:
        return
    sets.append("updated_at = ?")
    vals.append(now)
    vals.append(post_id)
    conn.execute(f"UPDATE posts SET {', '.join(sets)} WHERE post_id = ?", vals)
    conn.commit()


def _media_or_legacy(post: dict, media_rows: list[dict]) -> list[dict]:
    """The post's image list: post_media rows if any, else the legacy single
    image_path synthesised as a one-item list (so old posts still carry media)."""
    if media_rows:
        return media_rows
    if post.get("image_path"):
        return [{"post_id": post["post_id"], "ordinal": 0,
                 "path": post["image_path"], "alt": post.get("image_alt", "")}]
    return []


def get_post(conn: sqlite3.Connection, post_id: int) -> dict | None:
    row = conn.execute("SELECT * FROM posts WHERE post_id = ?", (post_id,)).fetchone()
    if not row:
        return None
    post = dict(row)
    post["media"] = _media_or_legacy(post, get_post_media(conn, post_id))
    post["mentions"] = get_post_mentions(conn, post_id)
    return post


def add_post_media(conn: sqlite3.Connection, *, post_id: int, ordinal: int,
                   path: str, alt: str = "") -> None:
    """Append one image to a post."""
    conn.execute(
        "INSERT INTO post_media (post_id, ordinal, path, alt) VALUES (?, ?, ?, ?)",
        (post_id, ordinal, path, alt),
    )
    conn.commit()


def get_post_media(conn: sqlite3.Connection, post_id: int) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM post_media WHERE post_id = ? ORDER BY ordinal, id", (post_id,)
    ).fetchall()
    return [dict(r) for r in rows]


def list_posts(conn: sqlite3.Connection, limit: int = 100) -> list[dict]:
    """Posts newest-first, each with its publications list attached."""
    # Thread parts (parent_post_id != 0) never appear as their own feed rows —
    # the parent carries a thread_count instead (gap-wave-3 §4).
    rows = conn.execute(
        "SELECT p.*, (SELECT COUNT(*) FROM posts c WHERE c.parent_post_id = p.post_id)"
        "   AS thread_count "
        "FROM posts p WHERE COALESCE(p.parent_post_id, 0) = 0 "
        "ORDER BY p.post_id DESC LIMIT ?", (limit,)
    ).fetchall()
    posts = [dict(r) for r in rows]
    if not posts:
        return []
    ids = [p["post_id"] for p in posts]
    ph = ",".join("?" * len(ids))
    pubs = conn.execute(
        f"SELECT * FROM post_publications WHERE post_id IN ({ph}) ORDER BY id", ids
    ).fetchall()
    by_post: dict[int, list] = {}
    for pub in pubs:
        by_post.setdefault(pub["post_id"], []).append(dict(pub))
    media_rows = conn.execute(
        f"SELECT * FROM post_media WHERE post_id IN ({ph}) ORDER BY post_id, ordinal, id", ids
    ).fetchall()
    media_by_post: dict[int, list] = {}
    for m in media_rows:
        media_by_post.setdefault(m["post_id"], []).append(dict(m))
    for p in posts:
        p["publications"] = by_post.get(p["post_id"], [])
        p["media"] = _media_or_legacy(p, media_by_post.get(p["post_id"], []))
    return posts


def delete_post(conn: sqlite3.Connection, post_id: int) -> None:
    conn.execute("DELETE FROM post_publications WHERE post_id = ?", (post_id,))
    conn.execute("DELETE FROM post_media WHERE post_id = ?", (post_id,))
    conn.execute("DELETE FROM post_mentions WHERE post_id = ?", (post_id,))
    conn.execute("DELETE FROM posts WHERE post_id = ?", (post_id,))
    conn.commit()


# ── handle-book (contacts) + post mentions ─────────────────────────
# A "contact" is a person you tag, carrying their handle on each platform.
# A post's mentions bind the @alias tokens in its body to contacts, so the
# publisher can expand each alias into the right per-platform handle.

_CONTACT_FIELDS = ("name", "handle_bsky", "handle_tw", "handle_mast", "handle_thr", "handle_tum")


def _clean_handle(v: str) -> str:
    """Store handles without a leading @ (the publisher re-adds it)."""
    return (v or "").strip().lstrip("@").strip()


def list_contacts(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM post_contacts ORDER BY name COLLATE NOCASE, id"
    ).fetchall()
    return [dict(r) for r in rows]


def get_contact(conn: sqlite3.Connection, contact_id: int) -> dict | None:
    row = conn.execute("SELECT * FROM post_contacts WHERE id = ?", (contact_id,)).fetchone()
    return dict(row) if row else None


def add_contact(conn: sqlite3.Connection, *, name: str, handle_bsky: str = "",
                handle_tw: str = "", handle_mast: str = "", handle_thr: str = "",
                handle_tum: str = "") -> int:
    cur = conn.execute(
        "INSERT INTO post_contacts (name, handle_bsky, handle_tw, handle_mast, handle_thr, handle_tum) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (name.strip(), _clean_handle(handle_bsky), _clean_handle(handle_tw),
         _clean_handle(handle_mast), _clean_handle(handle_thr), _clean_handle(handle_tum)),
    )
    conn.commit()
    return int(cur.lastrowid)


def update_contact(conn: sqlite3.Connection, contact_id: int, **fields) -> None:
    sets, vals = [], []
    for k, v in fields.items():
        if k not in _CONTACT_FIELDS:
            continue
        sets.append(f"{k} = ?")
        vals.append(v.strip() if k == "name" else _clean_handle(v))
    if not sets:
        return
    vals.append(contact_id)
    conn.execute(f"UPDATE post_contacts SET {', '.join(sets)} WHERE id = ?", vals)
    conn.commit()


def delete_contact(conn: sqlite3.Connection, contact_id: int) -> None:
    conn.execute("DELETE FROM post_contacts WHERE id = ?", (contact_id,))
    # Drop any bindings that referenced it; those @tokens revert to plain text.
    conn.execute("DELETE FROM post_mentions WHERE contact_id = ?", (contact_id,))
    conn.commit()


def set_post_mentions(conn: sqlite3.Connection, post_id: int,
                      bindings: list[dict]) -> None:
    """Replace a post's alias→contact bindings. Each binding: {token, contact_id}."""
    conn.execute("DELETE FROM post_mentions WHERE post_id = ?", (post_id,))
    seen = set()
    for b in bindings or []:
        token = (b.get("token") or "").strip().lstrip("@")
        cid = int(b.get("contact_id") or 0)
        if not token or not cid or token in seen:
            continue
        seen.add(token)
        conn.execute(
            "INSERT OR REPLACE INTO post_mentions (post_id, token, contact_id) VALUES (?, ?, ?)",
            (post_id, token, cid),
        )
    conn.commit()


def get_post_mentions(conn: sqlite3.Connection, post_id: int) -> list[dict]:
    """A post's bindings joined to their contact handles (LEFT JOIN so a deleted
    contact yields no handles → the alias stays plain text at publish)."""
    rows = conn.execute(
        "SELECT m.token, m.contact_id, c.name, "
        "       c.handle_bsky, c.handle_tw, c.handle_mast, c.handle_thr, c.handle_tum "
        "FROM post_mentions m LEFT JOIN post_contacts c ON c.id = m.contact_id "
        "WHERE m.post_id = ? ORDER BY m.id",
        (post_id,),
    ).fetchall()
    return [dict(r) for r in rows]


# ── post_publications ──────────────────────────────────────────────

def upsert_post_publication(conn: sqlite3.Connection, *, post_id: int, platform: str,
                            account_id: int = 0, status: str = "pending",
                            external_id: str = "", external_url: str = "",
                            error: str = "", now: str = "") -> int:
    """Insert or update the (post, platform, account) publication row."""
    conn.execute(
        "INSERT INTO post_publications "
        "(post_id, platform, account_id, status, external_id, external_url, error, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(post_id, platform, account_id) DO UPDATE SET "
        "status=excluded.status, external_id=excluded.external_id, "
        "external_url=excluded.external_url, error=excluded.error",
        (post_id, platform, account_id, status, external_id, external_url, error, now),
    )
    conn.commit()
    row = conn.execute(
        "SELECT id FROM post_publications WHERE post_id=? AND platform=? AND account_id=?",
        (post_id, platform, account_id),
    ).fetchone()
    return int(row["id"]) if row else 0


def get_post_publications(conn: sqlite3.Connection, post_id: int) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM post_publications WHERE post_id = ? ORDER BY id", (post_id,)
    ).fetchall()
    return [dict(r) for r in rows]


def get_thread_parts(conn: sqlite3.Connection, post_id: int) -> list[dict]:
    """A post's thread parts (children), in order (gap-wave-3 §4)."""
    rows = conn.execute(
        "SELECT * FROM posts WHERE parent_post_id = ? ORDER BY thread_ordinal, post_id",
        (post_id,),
    ).fetchall()
    return [dict(r) for r in rows]
