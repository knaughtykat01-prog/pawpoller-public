"""Data access for paired comments (spec 021) — the ``paired_comments`` table.

A paired comment is a reply from the same account straight under a post or a
piece's post. One row per (owner, chapter, site) holds BOTH the text as written
and how it went, so whichever way the post finally lands — Post now, a scheduled
slot, a retry row, a desktop hand-off — the comment is found by what it hangs
under, and no queue row has to carry it. Plain dicts out; no business logic here
(that is ``posting/paired_comment.py``).
"""
from __future__ import annotations

import json
import sqlite3

UNSENT = ("pending", "failed", "skipped", "cancelled")


def _row(r) -> dict | None:
    if r is None:
        return None
    d = dict(r)
    try:
        d["mentions"] = json.loads(d.get("mentions") or "[]")
    except (TypeError, ValueError):
        d["mentions"] = []
    return d


def put_pending(conn: sqlite3.Connection, kind: str, ref, chapter: int, platform: str,
                text: str, *, mentions=None, template: str = "", now: str = "") -> int | None:
    """Write (or rewrite) the comment for one owner + site as pending.

    A comment that already went up is left alone — editing after posting is out of
    scope, and overwriting would lose the link to the live reply. Returns the row id,
    or None when a posted row was kept."""
    ref = str(ref)
    cur = conn.execute(
        "SELECT id, status FROM paired_comments WHERE owner_kind=? AND owner_ref=? "
        "AND chapter_index=? AND platform=?", (kind, ref, int(chapter or 0), platform)).fetchone()
    m = json.dumps(mentions or [])
    if cur and cur["status"] in ("posted", "sending"):
        return None                       # live, or going up right now: leave it be
    if cur:
        conn.execute(
            "UPDATE paired_comments SET text=?, mentions=?, template=?, status='pending', error='', "
            "attempts=0, next_try_at='', updated_at=? WHERE id=?",
            (text, m, template, now, cur["id"]))
        conn.commit()
        return int(cur["id"])
    c = conn.execute(
        "INSERT INTO paired_comments (owner_kind, owner_ref, chapter_index, platform, text, mentions,"
        " template, status, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)",
        (kind, ref, int(chapter or 0), platform, text, m, template, now, now))
    conn.commit()
    return int(c.lastrowid)


def clear_unsent(conn: sqlite3.Connection, kind: str, ref, platforms=None, chapter: int | None = None) -> int:
    """Remove unsent rows for an owner (optionally only some sites / one chapter) —
    the composer's "no comment on this site" and a cleared dialog box."""
    sql = (f"DELETE FROM paired_comments WHERE owner_kind=? AND owner_ref=? "
           f"AND status IN ({','.join('?' * len(UNSENT))})")
    args: list = [kind, str(ref), *UNSENT]
    if chapter is not None:
        sql += " AND chapter_index=?"
        args.append(int(chapter))
    if platforms is not None:
        platforms = list(platforms)
        if not platforms:
            return 0
        sql += f" AND platform IN ({','.join('?' * len(platforms))})"
        args += platforms
    n = conn.execute(sql, args).rowcount
    conn.commit()
    return n


def get(conn: sqlite3.Connection, comment_id: int) -> dict | None:
    return _row(conn.execute("SELECT * FROM paired_comments WHERE id=?", (int(comment_id),)).fetchone())


def find(conn: sqlite3.Connection, kind: str, ref, chapter: int, platform: str) -> dict | None:
    return _row(conn.execute(
        "SELECT * FROM paired_comments WHERE owner_kind=? AND owner_ref=? AND chapter_index=? "
        "AND platform=?", (kind, str(ref), int(chapter or 0), platform)).fetchone())


def mark(conn: sqlite3.Connection, comment_id: int, status: str, *, now: str = "", **fields) -> None:
    allowed = ("account_id", "parent_external_id", "external_id", "external_url", "error",
               "attempts", "next_try_at")
    sets = ["status=?", "updated_at=?"]
    args: list = [status, now]
    for k, v in fields.items():
        if k in allowed:
            sets.append(f"{k}=?")
            args.append(v)
    args.append(int(comment_id))
    conn.execute(f"UPDATE paired_comments SET {', '.join(sets)} WHERE id=?", args)
    conn.commit()


def claim_due(conn: sqlite3.Connection, now: str, limit: int = 3) -> list[dict]:
    """Failed rows whose automatic retry is due, CLAIMED: next_try_at is cleared with a
    conditional UPDATE, so a desktop and a server sharing one database can't both send
    the same comment — the loser's UPDATE matches nothing."""
    rows = conn.execute(
        "SELECT id, next_try_at FROM paired_comments WHERE status='failed' AND next_try_at != '' "
        "AND next_try_at <= ? ORDER BY next_try_at LIMIT ?", (now, int(limit))).fetchall()
    out = []
    for r in rows:
        # updated_at = now: Retry reads "sending since" to tell a live send from a stuck one.
        n = conn.execute("UPDATE paired_comments SET next_try_at='', status='sending', updated_at=? "
                         "WHERE id=? AND next_try_at=? AND status='failed'",
                         (now, r["id"], r["next_try_at"])).rowcount
        if n:
            out.append(r["id"])
    conn.commit()
    return [get(conn, i) for i in out]


def claim(conn: sqlite3.Connection, comment_id: int, from_status: str, seen_updated_at: str,
          now: str = "") -> bool:
    """Take a row for sending (CMTRETRYCLAIM, 4.56.2): status -> 'sending' only if its status AND
    updated_at are still what the sender read — a compare-and-swap. Two senders (two Retry presses,
    the automatic retry, a publish's own pass) can't both win, including on a stuck 'sending' row
    that Retry is allowed to take back after 10 minutes, so a comment never goes up twice."""
    n = conn.execute("UPDATE paired_comments SET status='sending', updated_at=? "
                     "WHERE id=? AND status=? AND updated_at=?",
                     (now, int(comment_id), from_status, seen_updated_at or "")).rowcount
    conn.commit()
    return bool(n)


def rows_for_owner(conn: sqlite3.Connection, kind: str, ref) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM paired_comments WHERE owner_kind=? AND owner_ref=? ORDER BY chapter_index, platform",
        (kind, str(ref))).fetchall()
    return [_row(r) for r in rows]


def rows_for_posts(conn: sqlite3.Connection, post_ids) -> dict[int, dict[str, dict]]:
    """{post_id: {platform: row}} for many posts in ONE query (the feed list)."""
    ids = [str(int(p)) for p in post_ids or []]
    if not ids:
        return {}
    rows = conn.execute(
        f"SELECT * FROM paired_comments WHERE owner_kind='post' AND owner_ref IN ({','.join('?' * len(ids))})",
        ids).fetchall()
    out: dict[int, dict[str, dict]] = {}
    for r in rows:
        d = _row(r)
        out.setdefault(int(d["owner_ref"]), {})[d["platform"]] = d
    return out


def delete_unsent(conn: sqlite3.Connection, comment_id: int) -> bool:
    n = conn.execute(f"DELETE FROM paired_comments WHERE id=? AND status IN ({','.join('?' * len(UNSENT))})",
                     (int(comment_id), *UNSENT)).rowcount
    conn.commit()
    return bool(n)


def delete_for_post(conn: sqlite3.Connection, post_id: int) -> None:
    """A post deleted from PawPoller takes its comment rows with it (pieces never delete)."""
    conn.execute("DELETE FROM paired_comments WHERE owner_kind='post' AND owner_ref=?", (str(int(post_id)),))
    conn.commit()
