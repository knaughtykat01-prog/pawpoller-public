"""SQL for Facebook Page stats (`fb`) — spec 029 (4.59.0).

Posts on each connected Page (PawPoller's own and ones made on Facebook) with their numbers, a
snapshot whenever a number moves, and a poll log. NULL means Facebook gave no figure; a post gone
from Facebook is flagged ``deleted`` and kept (rows are never deleted). See database/fb_schema.sql.
"""
from __future__ import annotations

import json
import sqlite3

from database.scope import account_clause

_METRICS = ("views", "reactions", "comments", "shares", "plays")


# -- Posts ----------------------------------------------------------------------

def upsert_post(conn: sqlite3.Connection, post: dict, account_id: int, page_id: str) -> None:
    """Insert or refresh one post's details (not its numbers)."""
    text = post.get("message") or ""
    title = (text.strip().splitlines() or [""])[0][:120]
    media = (post.get("media_type") or "").lower()
    ctype = "video" if "video" in media else "photo" if media in ("photo", "album") else "link" if media in ("link", "share") else "post"
    video_id = post.get("target_id", "") if ctype == "video" else ""
    conn.execute(
        """INSERT INTO fb_submissions (submission_id, account_id, page_id, video_id, title, full_text, link,
                                       posted_at, content_type, deleted, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, datetime('now'))
           ON CONFLICT(submission_id) DO UPDATE SET
             account_id=excluded.account_id, page_id=excluded.page_id,
             video_id=COALESCE(NULLIF(excluded.video_id, ''), fb_submissions.video_id),
             title=excluded.title, full_text=excluded.full_text, link=excluded.link,
             posted_at=COALESCE(excluded.posted_at, fb_submissions.posted_at),
             content_type=excluded.content_type, deleted=0, updated_at=datetime('now')""",
        (post["id"], account_id, page_id, video_id, title, text, post.get("permalink_url") or "",
         post.get("created_time") or None, ctype))


def save_numbers(conn: sqlite3.Connection, post_id: str, row: dict, account_id: int, polled_at: str) -> bool:
    """Store a post's latest numbers and add a snapshot if any of them moved. Returns True if one did.

    A figure Facebook didn't give this time (None) keeps the last known value rather than wiping it."""
    old = conn.execute("SELECT views, reactions, comments, shares, plays FROM fb_submissions WHERE submission_id = ?",
                       (post_id,)).fetchone()
    new = {m: row.get(m) for m in _METRICS}
    if old is not None:
        for m in _METRICS:
            if new[m] is None:
                new[m] = old[m]
    conn.execute(
        """UPDATE fb_submissions SET views=?, reactions=?, comments=?, shares=?, plays=?,
           reactions_json = CASE WHEN ? = '{}' THEN reactions_json ELSE ? END, updated_at=datetime('now')
           WHERE submission_id=?""",
        (new["views"], new["reactions"], new["comments"], new["shares"], new["plays"],
         json.dumps(row.get("reactions_by_type") or {}), json.dumps(row.get("reactions_by_type") or {}), post_id))
    last = conn.execute("SELECT views, reactions, comments, shares, plays FROM fb_snapshots WHERE submission_id = ? "
                        "ORDER BY id DESC LIMIT 1", (post_id,)).fetchone()
    if last is not None and all(last[m] == new[m] for m in _METRICS):
        return False
    if all(new[m] is None for m in _METRICS):
        return False
    conn.execute("INSERT INTO fb_snapshots (account_id, submission_id, polled_at, views, reactions, comments, shares, "
                 "plays) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                 (account_id, post_id, polled_at, new["views"], new["reactions"], new["comments"], new["shares"],
                  new["plays"]))
    return True


def mark_gone(conn: sqlite3.Connection, account_id: int, seen_ids: set[str]) -> int:
    """After a full pass: posts on this account that Facebook no longer returned are flagged deleted
    (kept, numbers frozen). Never deletes a row."""
    rows = conn.execute("SELECT submission_id FROM fb_submissions WHERE account_id = ? AND deleted = 0",
                        (account_id,)).fetchall()
    gone = [r["submission_id"] for r in rows if r["submission_id"] not in seen_ids]
    for sid in gone:
        conn.execute("UPDATE fb_submissions SET deleted = 1, updated_at = datetime('now') WHERE submission_id = ?", (sid,))
    return len(gone)


def link_publications(conn: sqlite3.Connection) -> int:
    """Flag posts PawPoller published, and re-key video publications from the video id Facebook gave
    at upload to the post id the poll sees, so a piece's pooled totals find its Facebook video.
    Returns how many Library members were re-keyed."""
    moved = 0
    for r in conn.execute("SELECT submission_id, video_id FROM fb_submissions WHERE video_id != ''").fetchall():
        cur = conn.execute("UPDATE OR IGNORE masterpiece_members SET submission_id = ? "
                           "WHERE platform = 'fb' AND submission_id = ?", (r["submission_id"], r["video_id"]))
        moved += cur.rowcount or 0
    conn.execute("UPDATE fb_submissions SET made_by_pawpoller = 1 WHERE made_by_pawpoller = 0 AND submission_id IN "
                 "(SELECT submission_id FROM masterpiece_members WHERE platform = 'fb')")
    try:
        conn.execute("UPDATE fb_submissions SET made_by_pawpoller = 1 WHERE made_by_pawpoller = 0 AND submission_id IN "
                     "(SELECT external_id FROM post_publications WHERE platform = 'fb')")
    except sqlite3.OperationalError:
        pass    # no Posts tables on a very old install
    return moved


# -- Reads ------------------------------------------------------------------------

def get_post(conn: sqlite3.Connection, submission_id: str) -> dict | None:
    row = conn.execute("SELECT * FROM fb_submissions WHERE submission_id = ?", (submission_id,)).fetchone()
    return _shape(dict(row)) if row else None


def get_all_posts(conn: sqlite3.Connection, sort_by: str = "posted_at", order: str = "desc",
                  account_id: int | None = None) -> list[dict]:
    if sort_by not in {"posted_at", "title", "views", "reactions", "comments", "shares", "updated_at"}:
        sort_by = "posted_at"
    order_dir = "DESC" if order.lower() == "desc" else "ASC"
    where, params = account_clause(account_id)
    sql = "SELECT * FROM fb_submissions" + (f" WHERE {where}" if where else "")
    sql += f" ORDER BY {sort_by} IS NULL, {sort_by} {order_dir}"
    return [_shape(dict(r)) for r in conn.execute(sql, params).fetchall()]


def get_post_snapshots(conn: sqlite3.Connection, submission_id: str) -> list[dict]:
    return [dict(r) for r in conn.execute(
        "SELECT polled_at, views, reactions, comments, shares, plays FROM fb_snapshots WHERE submission_id = ? "
        "ORDER BY polled_at", (submission_id,)).fetchall()]


def _shape(row: dict) -> dict:
    try:
        row["reactions_by_type"] = json.loads(row.pop("reactions_json", "{}") or "{}")
    except ValueError:
        row["reactions_by_type"] = {}
    row["deleted"] = bool(row.get("deleted"))
    row["made_by_pawpoller"] = bool(row.get("made_by_pawpoller"))
    return row


def get_summary(conn: sqlite3.Connection, account_id: int | None = None) -> dict:
    """Totals over every post (deleted ones included: their numbers happened). A total is None when no
    post has that figure, so the page can say "not available" instead of 0."""
    where, params = account_clause(account_id)
    row = conn.execute(
        "SELECT COUNT(*) AS n, SUM(views) AS v, SUM(reactions) AS r, SUM(comments) AS c, SUM(shares) AS s, "
        "SUM(plays) AS p FROM fb_submissions" + (f" WHERE {where}" if where else ""), params).fetchone()
    from database import followers as followers_db
    fl = followers_db.platform_latest(conn, "fb", account_id)
    followers = fl["followers"] if fl else None
    return {"total_submissions": row["n"], "total_views": row["v"], "total_reactions": row["r"],
            "total_comments": row["c"], "total_shares": row["s"], "total_plays": row["p"], "followers": followers}


def get_aggregate(conn: sqlite3.Connection, start: str | None = None, end: str | None = None,
                  account_id: int | None = None) -> list[dict]:
    """Running totals per poll for the standard aggregate chart: each post's latest snapshot at or before
    each poll time, summed."""
    conds, params = [], []
    if start:
        conds.append("polled_at >= ?")
        params.append(start)
    if end:
        conds.append("polled_at <= ?")
        params.append(end)
    acc_sql, acc_params = account_clause(account_id)
    if acc_sql:
        conds.append(acc_sql)
        params.extend(acc_params)
    times = [r["polled_at"] for r in conn.execute(
        "SELECT DISTINCT polled_at FROM fb_snapshots" + (" WHERE " + " AND ".join(conds) if conds else "")
        + " ORDER BY polled_at", params).fetchall()]
    out = []
    for t in times:
        q = ("SELECT SUM(views) AS views, SUM(reactions) AS reactions, SUM(comments) AS comments, "
             "SUM(shares) AS shares FROM fb_snapshots s WHERE s.id IN (SELECT MAX(id) FROM fb_snapshots "
             "WHERE polled_at <= ?" + (f" AND {acc_sql}" if acc_sql else "") + " GROUP BY submission_id)")
        r = conn.execute(q, [t, *acc_params]).fetchone()
        out.append({"polled_at": t, **{k: r[k] for k in ("views", "reactions", "comments", "shares")}})
    return out


# -- Poll log -----------------------------------------------------------------------

def start_poll_log(conn: sqlite3.Connection, account_id: int = 0) -> int:
    cur = conn.execute("INSERT INTO fb_poll_log (started_at, status, account_id) VALUES (datetime('now'), 'running', ?)",
                       (account_id,))
    conn.commit()
    return cur.lastrowid


def finish_poll_log(conn: sqlite3.Connection, log_id: int, status: str, submissions_found: int = 0,
                    snapshots_inserted: int = 0, new_comments: int = 0, missing_permission: str = "",
                    error_message: str | None = None, duration_seconds: float = 0) -> None:
    conn.execute(
        """UPDATE fb_poll_log SET finished_at=datetime('now'), status=?, submissions_found=?, snapshots_inserted=?,
           new_comments=?, missing_permission=?, error_message=?, duration_seconds=? WHERE id=?""",
        (status, submissions_found, snapshots_inserted, new_comments, missing_permission or "", error_message,
         duration_seconds, log_id))
    conn.commit()


def get_fb_last_poll(conn: sqlite3.Connection) -> dict | None:
    row = conn.execute("SELECT * FROM fb_poll_log ORDER BY started_at DESC LIMIT 1").fetchone()
    return dict(row) if row else None


def get_poll_log(conn: sqlite3.Connection, limit: int = 50) -> list[dict]:
    return [dict(r) for r in conn.execute("SELECT * FROM fb_poll_log ORDER BY started_at DESC LIMIT ?",
                                          (limit,)).fetchall()]
