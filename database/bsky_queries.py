"""All SQL CRUD functions for the Bluesky (BSKY) analytics database.

Bluesky provides a free public API via the AT Protocol. Authentication
uses app passwords to obtain JWT sessions.

Key differences from other platforms:
  - submission_id is TEXT (AT URIs: at://did:plc:xxx/app.bsky.feed.post/yyy)
  - Stats: likes, reposts, replies, quotes (NO views metric)
  - No individual comment tracking — just counts
  - Content type is always 'post'
"""

from __future__ import annotations
import json
from database.scope import account_clause  # optional `account_id = ?` WHERE-injection
import sqlite3
from datetime import datetime, timezone
from typing import Any


# -- BSKY Submissions --------------------------------------------------------

def upsert_bsky_submission(conn: sqlite3.Connection, sub: dict, account_id: int) -> None:
    """Insert or update a Bluesky post's metadata and latest stats."""
    keywords_json = json.dumps(sub.get("keywords", []))
    # Full-res URL for every attached image (multi-image posts) as a JSON array;
    # '' when there are none. Powers all-images artwork import.
    media_urls_json = json.dumps(sub.get("media_urls", []) or [])
    # account_id set on INSERT only; the ON CONFLICT UPDATE leaves it alone.
    conn.execute(
        """INSERT INTO bsky_submissions
           (submission_id, account_id, title, full_text, username, posted_at, content_type,
            rating, description, keywords, link, thumbnail_url, media_urls,
            likes, reposts, replies, quotes, has_media, embed_type, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
           ON CONFLICT(submission_id) DO UPDATE SET
            title=excluded.title, full_text=excluded.full_text,
            username=excluded.username, content_type=excluded.content_type,
            rating=excluded.rating, description=excluded.description,
            keywords=excluded.keywords, link=excluded.link,
            thumbnail_url=excluded.thumbnail_url, media_urls=excluded.media_urls,
            likes=excluded.likes, reposts=excluded.reposts,
            replies=excluded.replies, quotes=excluded.quotes,
            has_media=excluded.has_media, embed_type=excluded.embed_type,
            updated_at=datetime('now')
        """,
        (
            sub["post_uri"], account_id, sub.get("title", ""), sub.get("full_text", ""),
            sub.get("username", ""), sub.get("posted_at"),
            sub.get("content_type", "post"), sub.get("rating", ""),
            sub.get("description", ""), keywords_json,
            sub.get("link", ""), sub.get("thumbnail_url", ""), media_urls_json,
            sub.get("likes", 0), sub.get("reposts", 0),
            sub.get("replies", 0), sub.get("quotes", 0),
            sub.get("has_media", 0), sub.get("embed_type", ""),
        ),
    )


def get_bsky_submission(conn: sqlite3.Connection, submission_id: str) -> dict | None:
    row = conn.execute("SELECT * FROM bsky_submissions WHERE submission_id = ?", (submission_id,)).fetchone()
    return dict(row) if row else None


def get_bsky_submission_by_rkey(conn: sqlite3.Connection, rkey: str) -> dict | None:
    """Find a submission by rkey (last segment of AT URI)."""
    row = conn.execute(
        "SELECT * FROM bsky_submissions WHERE submission_id LIKE ?",
        (f"%/{rkey}",),
    ).fetchone()
    return dict(row) if row else None


def get_all_bsky_submissions(conn: sqlite3.Connection, sort_by: str = "likes", order: str = "desc", account_id: int | None = None) -> list[dict]:
    allowed_sorts = {"likes", "reposts", "replies", "quotes",
                     "title", "posted_at", "updated_at"}
    if sort_by not in allowed_sorts:
        sort_by = "likes"
    order_dir = "DESC" if order.lower() == "desc" else "ASC"
    where, params = account_clause(account_id)
    sql = "SELECT * FROM bsky_submissions" + (f" WHERE {where}" if where else "")
    sql += f" ORDER BY {sort_by} {order_dir}"
    rows = conn.execute(sql, params).fetchall()
    return [dict(r) for r in rows]


# -- BSKY Snapshots ----------------------------------------------------------

def insert_bsky_snapshot(conn: sqlite3.Connection, account_id: int, submission_id: str,
                          likes: int, reposts: int, replies: int, quotes: int,
                          polled_at: str | None = None) -> None:
    ts = polled_at or datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    conn.execute(
        "INSERT INTO bsky_snapshots (account_id, submission_id, polled_at, likes, reposts, replies, quotes) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (account_id, submission_id, ts, likes, reposts, replies, quotes),
    )


def get_bsky_snapshots(conn: sqlite3.Connection, submission_id: str,
                        start: str | None = None, end: str | None = None) -> list[dict]:
    sql = "SELECT * FROM bsky_snapshots WHERE submission_id = ?"
    params: list[Any] = [submission_id]
    if start:
        sql += " AND polled_at >= ?"
        params.append(start)
    if end:
        sql += " AND polled_at <= ?"
        params.append(end)
    sql += " ORDER BY polled_at ASC"
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def get_bsky_aggregate_snapshots(conn: sqlite3.Connection, start: str | None = None,
                                  end: str | None = None, account_id: int | None = None) -> list[dict]:
    sql = ("SELECT polled_at, SUM(likes) as likes, SUM(reposts) as reposts, "
           "SUM(replies) as replies, SUM(quotes) as quotes "
           "FROM bsky_snapshots")
    params: list[Any] = []
    conditions = []
    if start:
        conditions.append("polled_at >= ?")
        params.append(start)
    if end:
        conditions.append("polled_at <= ?")
        params.append(end)
    acc_sql, acc_params = account_clause(account_id)
    if acc_sql:
        conditions.append(acc_sql)
        params.extend(acc_params)
    if conditions:
        sql += " WHERE " + " AND ".join(conditions)
    sql += " GROUP BY polled_at ORDER BY polled_at ASC"
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def get_bsky_comparison_snapshots(conn: sqlite3.Connection, submission_ids: list[str],
                                   start: str | None = None, end: str | None = None) -> dict[str, list[dict]]:
    """Multi-submission time-series. One IN-clause query instead of N SELECTs."""
    result: dict[str, list[dict]] = {sid: [] for sid in submission_ids}
    if not submission_ids:
        return result
    placeholders = ",".join("?" * len(submission_ids))
    sql = f"SELECT * FROM bsky_snapshots WHERE submission_id IN ({placeholders})"
    params: list[Any] = list(submission_ids)
    if start:
        sql += " AND polled_at >= ?"
        params.append(start)
    if end:
        sql += " AND polled_at <= ?"
        params.append(end)
    sql += " ORDER BY submission_id, polled_at ASC"
    for row in conn.execute(sql, params).fetchall():
        result[row["submission_id"]].append(dict(row))
    return result


# -- BSKY Poll Log -----------------------------------------------------------

def start_bsky_poll_log(conn: sqlite3.Connection, account_id: int = 0) -> int:
    cur = conn.execute(
        "INSERT INTO bsky_poll_log (started_at, status, account_id) VALUES (datetime('now'), 'running', ?)",
        (account_id,))
    conn.commit()
    return cur.lastrowid


def finish_bsky_poll_log(conn: sqlite3.Connection, log_id: int, status: str,
                          submissions_found: int = 0, snapshots_inserted: int = 0,
                          error_message: str | None = None, duration_seconds: float = 0) -> None:
    conn.execute(
        """UPDATE bsky_poll_log SET finished_at=datetime('now'), status=?, submissions_found=?,
           snapshots_inserted=?, error_message=?, duration_seconds=?
           WHERE id=?""",
        (status, submissions_found, snapshots_inserted,
         error_message, duration_seconds, log_id),
    )
    conn.commit()


def get_bsky_last_poll(conn: sqlite3.Connection) -> dict | None:
    row = conn.execute("SELECT * FROM bsky_poll_log ORDER BY started_at DESC LIMIT 1").fetchone()
    return dict(row) if row else None


def get_bsky_poll_log(conn: sqlite3.Connection, limit: int = 50) -> list[dict]:
    rows = conn.execute("SELECT * FROM bsky_poll_log ORDER BY started_at DESC LIMIT ?", (limit,)).fetchall()
    return [dict(r) for r in rows]


# -- BSKY Summary ------------------------------------------------------------

def get_bsky_summary(conn: sqlite3.Connection, account_id: int | None = None) -> dict:
    """Dashboard summary for Bluesky. With *account_id* set, the totals and
    top-lists are scoped to that account; account_id=None keeps the "All
    accounts" aggregate behaviour (byte-identical to before scoping)."""
    where, wp = account_clause(account_id)
    w = f" WHERE {where}" if where else ""
    totals = conn.execute(
        "SELECT COUNT(*) as total_submissions, COALESCE(SUM(likes),0) as total_likes, "
        "COALESCE(SUM(reposts),0) as total_reposts, "
        "COALESCE(SUM(replies),0) as total_replies, "
        "COALESCE(SUM(quotes),0) as total_quotes "
        "FROM bsky_submissions" + w,
        wp,
    ).fetchone()
    totals = dict(totals)

    top_liked = conn.execute(
        "SELECT submission_id, title, likes FROM bsky_submissions" + w + " ORDER BY likes DESC LIMIT 5",
        wp,
    ).fetchall()

    top_reposted = conn.execute(
        "SELECT submission_id, title, reposts FROM bsky_submissions" + w + " ORDER BY reposts DESC LIMIT 5",
        wp,
    ).fetchall()

    # Fastest-growing: only the outer `s` needs scoping — submission_ids are
    # unique to their account, so the snapshot subquery is implicitly correct.
    sw, sp = account_clause(account_id, "s")
    fastest_growing = conn.execute(
        """SELECT s.submission_id, s.title,
                  COALESCE(s.likes - oldest.likes, 0) as likes_gained,
                  COALESCE(s.reposts - oldest.reposts, 0) as reposts_gained
           FROM bsky_submissions s
           LEFT JOIN (
               SELECT s1.submission_id, s1.likes, s1.reposts
               FROM bsky_snapshots s1
               INNER JOIN (
                   SELECT submission_id, MAX(polled_at) as max_polled
                   FROM bsky_snapshots
                   WHERE polled_at <= datetime('now', '-24 hours')
                   GROUP BY submission_id
               ) s2 ON s1.submission_id = s2.submission_id AND s1.polled_at = s2.max_polled
           ) oldest ON s.submission_id = oldest.submission_id
           WHERE """ + (sw + " AND " if sw else "") + """COALESCE(s.likes - oldest.likes, 0) > 0
           ORDER BY likes_gained DESC LIMIT 5""",
        sp,
    ).fetchall()

    return {
        "total_submissions": totals["total_submissions"],
        "total_likes": totals["total_likes"],
        "total_reposts": totals["total_reposts"],
        "total_replies": totals["total_replies"],
        "total_quotes": totals["total_quotes"],
        "top_liked": [dict(r) for r in top_liked],
        "top_reposted": [dict(r) for r in top_reposted],
        "fastest_growing": [dict(r) for r in fastest_growing],
    }


# -- BSKY Growth Rates -------------------------------------------------------

def _calc_growth_rate(current: int, past: int | None, hours: int) -> float | None:
    if past is None:
        return None
    delta = current - past
    days = hours / 24.0
    return round(delta / days, 2) if days > 0 else None


def get_bsky_growth_rates(conn: sqlite3.Connection) -> dict:
    totals = conn.execute(
        "SELECT COALESCE(SUM(likes),0) as likes, "
        "COALESCE(SUM(reposts),0) as reposts, "
        "COALESCE(SUM(replies),0) as replies "
        "FROM bsky_submissions"
    ).fetchone()
    current_likes = totals["likes"]
    current_reposts = totals["reposts"]
    current_replies = totals["replies"]

    rates = {}
    for label, hours in [("24h", 24), ("7d", 168), ("30d", 720)]:
        row = conn.execute(
            """SELECT SUM(likes) as likes, SUM(reposts) as reposts,
                      SUM(replies) as replies
               FROM bsky_snapshots WHERE polled_at = (
                   SELECT polled_at FROM bsky_snapshots
                   WHERE polled_at <= datetime('now', ? || ' hours')
                   ORDER BY polled_at DESC LIMIT 1
               )""",
            (str(-hours),),
        ).fetchone()
        past_likes = row["likes"] if row and row["likes"] is not None else None
        past_reposts = row["reposts"] if row and row["reposts"] is not None else None
        past_replies = row["replies"] if row and row["replies"] is not None else None
        rates[label] = {
            "likes_per_day": _calc_growth_rate(current_likes, past_likes, hours),
            "reposts_per_day": _calc_growth_rate(current_reposts, past_reposts, hours),
            "replies_per_day": _calc_growth_rate(current_replies, past_replies, hours),
        }
    return rates


def get_bsky_submission_growth_rates(conn: sqlite3.Connection, submission_id: str) -> dict:
    sub = conn.execute(
        "SELECT likes, reposts, replies FROM bsky_submissions WHERE submission_id = ?",
        (submission_id,),
    ).fetchone()
    if not sub:
        return {}

    rates = {}
    for label, hours in [("24h", 24), ("7d", 168), ("30d", 720)]:
        row = conn.execute(
            """SELECT likes, reposts, replies
               FROM bsky_snapshots WHERE submission_id = ? AND polled_at <= datetime('now', ? || ' hours')
               ORDER BY polled_at DESC LIMIT 1""",
            (submission_id, str(-hours)),
        ).fetchone()
        past_likes = row["likes"] if row else None
        past_reposts = row["reposts"] if row else None
        past_replies = row["replies"] if row else None
        rates[label] = {
            "likes_per_day": _calc_growth_rate(sub["likes"], past_likes, hours),
            "reposts_per_day": _calc_growth_rate(sub["reposts"], past_reposts, hours),
            "replies_per_day": _calc_growth_rate(sub["replies"], past_replies, hours),
        }
    return rates


def get_bsky_submission_deltas(conn: sqlite3.Connection) -> dict[str, dict]:
    rows = conn.execute(
        """SELECT s.submission_id,
                  COALESCE(s.likes - old.likes, 0) as likes_delta,
                  COALESCE(s.reposts - old.reposts, 0) as reposts_delta,
                  COALESCE(s.replies - old.replies, 0) as replies_delta,
                  COALESCE(s.quotes - old.quotes, 0) as quotes_delta
           FROM bsky_submissions s
           LEFT JOIN (
               SELECT s1.submission_id, s1.likes, s1.reposts, s1.replies, s1.quotes
               FROM bsky_snapshots s1
               INNER JOIN (
                   SELECT submission_id, MAX(polled_at) as max_polled
                   FROM bsky_snapshots
                   WHERE polled_at <= datetime('now', '-24 hours')
                   GROUP BY submission_id
               ) s2 ON s1.submission_id = s2.submission_id AND s1.polled_at = s2.max_polled
           ) old ON s.submission_id = old.submission_id"""
    ).fetchall()
    return {r["submission_id"]: dict(r) for r in rows}
