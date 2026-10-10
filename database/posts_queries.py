"""Data-access helpers for the Posts (microblog) module — 2.49.0.

Thin CRUD over the ``posts`` + ``post_publications`` tables. No business logic
lives here (that's ``posting/post_publisher.py``); these just read/write rows
and hand back plain dicts. Timestamps are supplied by the caller so the pure
helpers stay side-effect free and testable.
"""
from __future__ import annotations

import json
import re
import sqlite3


# ── posts ──────────────────────────────────────────────────────────

def create_post(conn: sqlite3.Connection, *, body: str, rating: str = "general",
                image_path: str = "", image_alt: str = "", now: str = "",
                parent_post_id: int = 0, thread_ordinal: int = 0,
                kind: str = "post", title: str = "", tags: str = "", featured: bool = False) -> int:
    """Insert a draft post and return its post_id. parent_post_id/thread_ordinal
    make the row a thread PART (gap-wave-3 §4); 0 = a top-level post. ``kind='journal'``
    (spec 027) makes it a journal: a title, tags and FA's featured tick."""
    cur = conn.execute(
        "INSERT INTO posts (body, rating, image_path, image_alt, created_at, updated_at,"
        " parent_post_id, thread_ordinal, kind, title, tags, featured) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (body, rating, image_path, image_alt, now, now,
         int(parent_post_id or 0), int(thread_ordinal or 0),
         "journal" if kind == "journal" else "post", title, tags, 1 if featured else 0),
    )
    conn.commit()
    return int(cur.lastrowid)


def update_post(conn: sqlite3.Connection, post_id: int, *, now: str = "", **fields) -> None:
    """Patch a post's editable columns (body/rating/image_path/image_alt; spec 021's
    linked_kind/linked_ref — the piece its paired comment's placeholders fill from)."""
    allowed = {"body", "rating", "image_path", "image_alt", "linked_kind", "linked_ref",
               "title", "tags", "featured"}                  # spec 027 journals
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


# Spec 018 (FR-002). A scheduled post is one with a PENDING queue row; a failed post
# has a site that failed with no success on that same site (a failure on one account
# and a success on another means it posted). Both are correlated on `p`.
_SCHEDULED_SQL = ("EXISTS (SELECT 1 FROM posting_queue sq WHERE sq.content_type = 'post' "
                  "AND sq.status = 'pending' AND sq.story_name = CAST(p.post_id AS TEXT))")
_FAILED_SQL = ("EXISTS (SELECT 1 FROM post_publications f WHERE f.post_id = p.post_id "
               "AND f.status = 'failed' AND NOT EXISTS (SELECT 1 FROM post_publications s "
               "WHERE s.post_id = p.post_id AND s.platform = f.platform AND s.status = 'posted'))")
# A persona's post: one of its publications went out on that persona's account, or a
# pending schedule was made for the persona (or on one of its accounts).
_PERSONA_SQL = ("(EXISTS (SELECT 1 FROM post_publications pp JOIN accounts a "
                "ON a.account_id = pp.account_id WHERE pp.post_id = p.post_id AND a.persona_id = ?) "
                "OR EXISTS (SELECT 1 FROM posting_queue pq LEFT JOIN accounts qa "
                "ON qa.account_id = pq.account_id WHERE pq.content_type = 'post' "
                "AND pq.status = 'pending' AND pq.story_name = CAST(p.post_id AS TEXT) "
                "AND (pq.persona_id = ? OR qa.persona_id = ?)))")
_TOP_LEVEL = "COALESCE(p.parent_post_id, 0) = 0"


def _like(text: str) -> str:
    """A LIKE pattern that matches `text` literally (``%`` and ``_`` are not wildcards)."""
    esc = text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{esc}%"


def post_counts(conn: sqlite3.Connection) -> dict:
    """How many top-level posts are scheduled, and how many failed somewhere (the filter chips)."""
    def n(cond: str) -> int:
        return int(conn.execute(
            f"SELECT COUNT(*) FROM posts p WHERE {_TOP_LEVEL} AND {cond}").fetchone()[0])
    return {"scheduled": n(_SCHEDULED_SQL), "failed": n(_FAILED_SQL)}


def pending_post_schedules(conn: sqlite3.Connection, post_ids=None) -> list[dict]:
    """Pending queue rows for posts (all of them, or these post ids), soonest first."""
    sql = ("SELECT queue_id, story_name, platform, account_id, scheduled_at, persona_id "
           "FROM posting_queue WHERE content_type = 'post' AND status = 'pending'")
    args: list = []
    if post_ids is not None:
        if not post_ids:
            return []
        sql += f" AND story_name IN ({','.join('?' * len(post_ids))})"
        args = [str(i) for i in post_ids]
    out = []
    for r in conn.execute(sql + " ORDER BY scheduled_at, queue_id", args).fetchall():
        d = dict(r)
        try:
            d["post_id"] = int(d.pop("story_name"))
        except (TypeError, ValueError):
            continue
        out.append(d)
    return out


def list_posts(conn: sqlite3.Connection, limit: int = 100, *, status: str | None = None,
               persona_id: int | None = None, q: str | None = None,
               kind: str | None = None) -> list[dict]:
    """Posts newest-first, each with its publications, media and pending schedule rows.

    Filters (spec 018): ``status`` 'scheduled' | 'failed', ``persona_id``, and ``q``, a
    literal search of the body; ``kind`` 'post' | 'journal' (spec 027)."""
    # Thread parts (parent_post_id != 0) never appear as their own feed rows —
    # the parent carries a thread_count instead (gap-wave-3 §4).
    where, args = [_TOP_LEVEL], []
    if status == "scheduled":
        where.append(_SCHEDULED_SQL)
    elif status == "failed":
        where.append(_FAILED_SQL)
    if persona_id:
        where.append(_PERSONA_SQL)
        args += [int(persona_id)] * 3
    if q and q.strip():
        where.append("(p.body LIKE ? ESCAPE '\\' OR p.title LIKE ? ESCAPE '\\')")
        args += [_like(q.strip())] * 2
    if kind in ("post", "journal"):
        where.append("p.kind = ?")
        args.append(kind)
    rows = conn.execute(
        "SELECT p.*, (SELECT COUNT(*) FROM posts c WHERE c.parent_post_id = p.post_id)"
        "   AS thread_count "
        f"FROM posts p WHERE {' AND '.join(where)} "
        "ORDER BY p.post_id DESC LIMIT ?", (*args, limit)
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
    sched_by_post: dict[int, list] = {}
    for s in pending_post_schedules(conn, ids):
        sched_by_post.setdefault(s["post_id"], []).append(s)
    for p in posts:
        p["publications"] = by_post.get(p["post_id"], [])
        p["media"] = _media_or_legacy(p, media_by_post.get(p["post_id"], []))
        p["scheduled"] = sched_by_post.get(p["post_id"], [])
    return posts


def delete_post(conn: sqlite3.Connection, post_id: int) -> None:
    conn.execute("DELETE FROM post_publications WHERE post_id = ?", (post_id,))
    conn.execute("DELETE FROM post_media WHERE post_id = ?", (post_id,))
    conn.execute("DELETE FROM post_mentions WHERE post_id = ?", (post_id,))
    # Spec 021: its paired comments go with it (pieces never delete; posts may).
    conn.execute("DELETE FROM paired_comments WHERE owner_kind = 'post' AND owner_ref = ?", (str(post_id),))
    conn.execute("DELETE FROM posts WHERE post_id = ?", (post_id,))
    conn.commit()


# ── handle-book (contacts) + post mentions ─────────────────────────
# A "contact" is a person you tag, carrying their handle on each platform.
# A post's mentions bind the @alias tokens in its body to contacts, so the
# publisher can expand each alias into the right per-platform handle.

_CONTACT_FIELDS = ("name", "alias", "handle_bsky", "handle_tw", "handle_mast", "handle_thr",
                   "handle_tum")


def _clean_handle(v: str) -> str:
    """Store handles without a leading @ (the publisher re-adds it)."""
    return (v or "").strip().lstrip("@").strip()


def _contact_out(row) -> dict:
    d = dict(row)
    try:
        d["checks"] = json.loads(d.pop("handle_checks", None) or "{}")
    except (TypeError, ValueError):
        d["checks"] = {}
    return d


def list_contacts(conn: sqlite3.Connection) -> list[dict]:
    """Every contact, with ``used_count`` = how many posts tag them (spec 018)."""
    rows = conn.execute(
        "SELECT c.*, (SELECT COUNT(DISTINCT m.post_id) FROM post_mentions m "
        "             WHERE m.contact_id = c.id) AS used_count "
        "FROM post_contacts c ORDER BY c.name COLLATE NOCASE, c.id"
    ).fetchall()
    return [_contact_out(r) for r in rows]


def get_contact(conn: sqlite3.Connection, contact_id: int) -> dict | None:
    row = conn.execute("SELECT * FROM post_contacts WHERE id = ?", (contact_id,)).fetchone()
    return _contact_out(row) if row else None


def _clean_alias(v: str) -> str:
    return (v or "").strip().lstrip("@").strip()


def tag_conflict(conn: sqlite3.Connection, name: str, alias: str,
                 exclude_id: int | None = None) -> str:
    """Why a contact's @tag would collide with another contact's, or ''.

    A contact answers to its alias and to its name; the composer binds a typed @token
    to the contact that answers to it, so two contacts answering to one word would turn
    that binding into a guess."""
    mine = {t.lower() for t in (_clean_alias(alias), (name or "").strip()) if t}
    for c in conn.execute("SELECT id, name, alias FROM post_contacts").fetchall():
        if exclude_id is not None and c["id"] == exclude_id:
            continue
        theirs = {t.lower() for t in (_clean_alias(c["alias"]), (c["name"] or "").strip()) if t}
        clash = mine & theirs
        if clash:
            return f"@{sorted(clash)[0]} already tags {c['name']}"
    return ""


def add_contact(conn: sqlite3.Connection, *, name: str, alias: str = "", handle_bsky: str = "",
                handle_tw: str = "", handle_mast: str = "", handle_thr: str = "",
                handle_tum: str = "") -> int:
    cur = conn.execute(
        "INSERT INTO post_contacts (name, alias, handle_bsky, handle_tw, handle_mast, handle_thr,"
        " handle_tum) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (name.strip(), _clean_alias(alias), _clean_handle(handle_bsky), _clean_handle(handle_tw),
         _clean_handle(handle_mast), _clean_handle(handle_thr), _clean_handle(handle_tum)),
    )
    conn.commit()
    return int(cur.lastrowid)


def set_contact_checks(conn: sqlite3.Connection, contact_id: int, checks: dict) -> None:
    conn.execute("UPDATE post_contacts SET handle_checks = ? WHERE id = ?",
                 (json.dumps(checks), contact_id))
    conn.commit()


# An @name that is not inside a word: "mail me@example.com" is an address, not a tag.
_MENTION_RE = re.compile(r"(?<![\w@.])@(\w+)")


def suggest_contacts(conn: sqlite3.Connection, limit: int = 20) -> list[dict]:
    """@names in the operator's own posts that no contact answers to yet (spec 018).

    Spelling only, no guessing: a name counts once per post (thread parts included) and
    matches case-insensitively; the first spelling seen is the one offered."""
    taken = set()
    for c in conn.execute("SELECT name, alias FROM post_contacts").fetchall():
        for t in (_clean_alias(c["alias"]), (c["name"] or "").strip()):
            if t:
                taken.add(t.lower())
    counts: dict[str, int] = {}
    spelled: dict[str, str] = {}
    for (body,) in conn.execute("SELECT body FROM posts WHERE body LIKE '%@%'").fetchall():
        seen = set()
        for m in _MENTION_RE.finditer(body or ""):
            key = m.group(1).lower()
            if key in taken or key in seen:
                continue
            seen.add(key)
            counts[key] = counts.get(key, 0) + 1
            spelled.setdefault(key, m.group(1))
    top = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[:limit]
    return [{"name": spelled[k], "count": n} for k, n in top]


def update_contact(conn: sqlite3.Connection, contact_id: int, **fields) -> None:
    sets, vals = [], []
    for k, v in fields.items():
        if k not in _CONTACT_FIELDS:
            continue
        sets.append(f"{k} = ?")
        vals.append(v.strip() if k == "name" else _clean_alias(v) if k == "alias"
                    else _clean_handle(v))
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
