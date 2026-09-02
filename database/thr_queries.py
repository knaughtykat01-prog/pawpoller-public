"""All SQL CRUD functions for the Threads (THR) analytics database.

Official Threads Graph API. Metrics: views, likes, reposts, replies, quotes.
  - submission_id is TEXT (numeric media id)
  - content_type is text / image / video / carousel / quote / repost
"""

from __future__ import annotations
import json
from database.scope import account_clause  # optional `account_id = ?` WHERE-injection
import sqlite3
from datetime import datetime, timezone
from typing import Any


# -- THR Submissions ---------------------------------------------------------

def upsert_thr_submission(conn: sqlite3.Connection, sub: dict, account_id: int) -> None:
    keywords_json = json.dumps(sub.get("keywords", []))
    conn.execute(
        """INSERT INTO thr_submissions
           (submission_id, account_id, title, full_text, username, posted_at, content_type,
            rating, description, keywords, link, thumbnail_url,
            views, likes, reposts, replies, quotes, has_media, embed_type, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
           ON CONFLICT(submission_id) DO UPDATE SET
            title=excluded.title, full_text=excluded.full_text,
            username=excluded.username, content_type=excluded.content_type,
            rating=excluded.rating, description=excluded.description,
            keywords=excluded.keywords, link=excluded.link,
            thumbnail_url=excluded.thumbnail_url,
            views=excluded.views, likes=excluded.likes, reposts=excluded.reposts,
            replies=excluded.replies, quotes=excluded.quotes,
            has_media=excluded.has_media, embed_type=excluded.embed_type,
            updated_at=datetime('now')
        """,
        (
            sub["post_uri"], account_id, sub.get("title", ""), sub.get("full_text", ""),
            sub.get("username", ""), sub.get("posted_at"),
            sub.get("content_type", "text"), sub.get("rating", ""),
            sub.get("description", ""), keywords_json,
            sub.get("link", ""), sub.get("thumbnail_url", ""),
            sub.get("views", 0), sub.get("likes", 0), sub.get("reposts", 0),
            sub.get("replies", 0), sub.get("quotes", 0),
            sub.get("has_media", 0), sub.get("embed_type", ""),
        ),
    )


def get_thr_submission(conn: sqlite3.Connection, submission_id: str) -> dict | None:
    row = conn.execute("SELECT * FROM thr_submissions WHERE submission_id = ?", (submission_id,)).fetchone()
    return dict(row) if row else None


def get_all_thr_submissions(conn: sqlite3.Connection, sort_by: str = "views", order: str = "desc", account_id: int | None = None) -> list[dict]:
    allowed_sorts = {"views", "likes", "reposts", "replies", "quotes",
                     "title", "posted_at", "updated_at"}
    if sort_by not in allowed_sorts:
        sort_by = "views"
    order_dir = "DESC" if order.lower() == "desc" else "ASC"
    where, params = account_clause(account_id)
    sql = "SELECT * FROM thr_submissions" + (f" WHERE {where}" if where else "")
    sql += f" ORDER BY {sort_by} {order_dir}"
    rows = conn.execute(sql, params).fetchall()
    return [dict(r) for r in rows]


# -- THR Snapshots -----------------------------------------------------------

def insert_thr_snapshot(conn: sqlite3.Connection, account_id: int, submission_id: str,
                        views: int, likes: int, reposts: int, replies: int, quotes: int,
                        polled_at: str | None = None) -> None:
    ts = polled_at or datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    conn.execute(
        "INSERT INTO thr_snapshots (account_id, submission_id, polled_at, views, likes, reposts, replies, quotes) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (account_id, submission_id, ts, views, likes, reposts, replies, quotes),
    )


def get_thr_snapshots(conn: sqlite3.Connection, submission_id: str,
                      start: str | None = None, end: str | None = None) -> list[dict]:
    sql = "SELECT * FROM thr_snapshots WHERE submission_id = ?"
    params: list[Any] = [submission_id]
    if start:
        sql += " AND polled_at >= ?"
        params.append(start)
    if end:
        sql += " AND polled_at <= ?"
        params.append(end)
    sql += " ORDER BY polled_at ASC"
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def get_thr_aggregate_snapshots(conn: sqlite3.Connection, start: str | None = None,
                                end: str | None = None, account_id: int | None = None) -> list[dict]:
    sql = ("SELECT polled_at, SUM(views) as views, SUM(likes) as likes, SUM(reposts) as reposts, "
           "SUM(replies) as replies, SUM(quotes) as quotes FROM thr_snapshots")
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


def get_thr_comparison_snapshots(conn: sqlite3.Connection, submission_ids: list[str],
                                 start: str | None = None, end: str | None = None) -> dict[str, list[dict]]:
    result: dict[str, list[dict]] = {sid: [] for sid in submission_ids}
    if not submission_ids:
        return result
    placeholders = ",".join("?" * len(submission_ids))
    sql = f"SELECT * FROM thr_snapshots WHERE submission_id IN ({placeholders})"
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


# -- THR Poll Log ------------------------------------------------------------

def start_thr_poll_log(conn: sqlite3.Connection, account_id: int = 0) -> int:
    cur = conn.execute(
        "INSERT INTO thr_poll_log (started_at, status, account_id) VALUES (datetime('now'), 'running', ?)",
        (account_id,))
    conn.commit()
    return cur.lastrowid


def finish_thr_poll_log(conn: sqlite3.Connection, log_id: int, status: str,
                        submissions_found: int = 0, snapshots_inserted: int = 0,
                        error_message: str | None = None, duration_seconds: float = 0) -> None:
    conn.execute(
        """UPDATE thr_poll_log SET finished_at=datetime('now'), status=?, submissions_found=?,
           snapshots_inserted=?, error_message=?, duration_seconds=?
           WHERE id=?""",
        (status, submissions_found, snapshots_inserted,
         error_message, duration_seconds, log_id),
    )
    conn.commit()


def get_thr_last_poll(conn: sqlite3.Connection) -> dict | None:
    row = conn.execute("SELECT * FROM thr_poll_log ORDER BY started_at DESC LIMIT 1").fetchone()
    return dict(row) if row else None


def get_thr_poll_log(conn: sqlite3.Connection, limit: int = 50) -> list[dict]:
    rows = conn.execute("SELECT * FROM thr_poll_log ORDER BY started_at DESC LIMIT ?", (limit,)).fetchall()
    return [dict(r) for r in rows]


# -- THR Summary -------------------------------------------------------------

def get_thr_summary(conn: sqlite3.Connection, account_id: int | None = None) -> dict:
    where, wp = account_clause(account_id)
    w = f" WHERE {where}" if where else ""
    totals = conn.execute(
        "SELECT COUNT(*) as total_submissions, COALESCE(SUM(views),0) as total_views, "
        "COALESCE(SUM(likes),0) as total_likes, COALESCE(SUM(reposts),0) as total_reposts, "
        "COALESCE(SUM(replies),0) as total_replies, COALESCE(SUM(quotes),0) as total_quotes "
        "FROM thr_submissions" + w,
        wp,
    ).fetchone()
    totals = dict(totals)

    top_viewed = conn.execute(
        "SELECT submission_id, title, views FROM thr_submissions" + w + " ORDER BY views DESC LIMIT 5",
        wp,
    ).fetchall()

    top_liked = conn.execute(
        "SELECT submission_id, title, likes FROM thr_submissions" + w + " ORDER BY likes DESC LIMIT 5",
        wp,
    ).fetchall()

    sw, sp = account_clause(account_id, "s")
    fastest_growing = conn.execute(
        """SELECT s.submission_id, s.title,
                  COALESCE(s.views - oldest.views, 0) as views_gained,
                  COALESCE(s.likes - oldest.likes, 0) as likes_gained
           FROM thr_submissions s
           LEFT JOIN (
               SELECT s1.submission_id, s1.views, s1.likes
               FROM thr_snapshots s1
               INNER JOIN (
                   SELECT submission_id, MAX(polled_at) as max_polled
                   FROM thr_snapshots
                   WHERE polled_at <= datetime('now', '-24 hours')
                   GROUP BY submission_id
               ) s2 ON s1.submission_id = s2.submission_id AND s1.polled_at = s2.max_polled
           ) oldest ON s.submission_id = oldest.submission_id
           WHERE """ + (sw + " AND " if sw else "") + """COALESCE(s.views - oldest.views, 0) > 0
           ORDER BY views_gained DESC LIMIT 5""",
        sp,
    ).fetchall()

    return {
        "total_submissions": totals["total_submissions"],
        "total_views": totals["total_views"],
        "total_likes": totals["total_likes"],
        "total_reposts": totals["total_reposts"],
        "total_replies": totals["total_replies"],
        "total_quotes": totals["total_quotes"],
        # cross-platform aggregation reads total_favorites as the engagement bucket
        "total_favorites": totals["total_likes"],
        "top_viewed": [dict(r) for r in top_viewed],
        "top_liked": [dict(r) for r in top_liked],
        "fastest_growing": [dict(r) for r in fastest_growing],
    }


# -- THR Growth Rates --------------------------------------------------------

def _calc_growth_rate(current: int, past: int | None, hours: int) -> float | None:
    if past is None:
        return None
    delta = current - past
    days = hours / 24.0
    return round(delta / days, 2) if days > 0 else None


def get_thr_growth_rates(conn: sqlite3.Connection) -> dict:
    totals = conn.execute(
        "SELECT COALESCE(SUM(views),0) as views, COALESCE(SUM(likes),0) as likes, "
        "COALESCE(SUM(replies),0) as replies FROM thr_submissions"
    ).fetchone()
    current_views = totals["views"]
    current_likes = totals["likes"]
    current_replies = totals["replies"]

    rates = {}
    for label, hours in [("24h", 24), ("7d", 168), ("30d", 720)]:
        row = conn.execute(
            """SELECT SUM(views) as views, SUM(likes) as likes, SUM(replies) as replies
               FROM thr_snapshots WHERE polled_at = (
                   SELECT polled_at FROM thr_snapshots
                   WHERE polled_at <= datetime('now', ? || ' hours')
                   ORDER BY polled_at DESC LIMIT 1
               )""",
            (str(-hours),),
        ).fetchone()
        past_views = row["views"] if row and row["views"] is not None else None
        past_likes = row["likes"] if row and row["likes"] is not None else None
        past_replies = row["replies"] if row and row["replies"] is not None else None
        rates[label] = {
            "views_per_day": _calc_growth_rate(current_views, past_views, hours),
            "faves_per_day": _calc_growth_rate(current_likes, past_likes, hours),
            "comments_per_day": _calc_growth_rate(current_replies, past_replies, hours),
        }
    return rates


def get_thr_submission_growth_rates(conn: sqlite3.Connection, submission_id: str) -> dict:
    sub = conn.execute(
        "SELECT views, likes, replies FROM thr_submissions WHERE submission_id = ?",
        (submission_id,),
    ).fetchone()
    if not sub:
        return {}

    rates = {}
    for label, hours in [("24h", 24), ("7d", 168), ("30d", 720)]:
        row = conn.execute(
            """SELECT views, likes, replies
               FROM thr_snapshots WHERE submission_id = ? AND polled_at <= datetime('now', ? || ' hours')
               ORDER BY polled_at DESC LIMIT 1""",
            (submission_id, str(-hours)),
        ).fetchone()
        past_views = row["views"] if row else None
        past_likes = row["likes"] if row else None
        past_replies = row["replies"] if row else None
        rates[label] = {
            "views_per_day": _calc_growth_rate(sub["views"], past_views, hours),
            "faves_per_day": _calc_growth_rate(sub["likes"], past_likes, hours),
            "comments_per_day": _calc_growth_rate(sub["replies"], past_replies, hours),
        }
    return rates


def get_thr_submission_deltas(conn: sqlite3.Connection) -> dict[str, dict]:
    rows = conn.execute(
        """SELECT s.submission_id,
                  COALESCE(s.views - old.views, 0) as views_delta,
                  COALESCE(s.likes - old.likes, 0) as likes_delta,
                  COALESCE(s.reposts - old.reposts, 0) as reposts_delta,
                  COALESCE(s.replies - old.replies, 0) as replies_delta,
                  COALESCE(s.quotes - old.quotes, 0) as quotes_delta
           FROM thr_submissions s
           LEFT JOIN (
               SELECT s1.submission_id, s1.views, s1.likes, s1.reposts, s1.replies, s1.quotes
               FROM thr_snapshots s1
               INNER JOIN (
                   SELECT submission_id, MAX(polled_at) as max_polled
                   FROM thr_snapshots
                   WHERE polled_at <= datetime('now', '-24 hours')
                   GROUP BY submission_id
               ) s2 ON s1.submission_id = s2.submission_id AND s1.polled_at = s2.max_polled
           ) old ON s.submission_id = old.submission_id"""
    ).fetchall()
    return {r["submission_id"]: dict(r) for r in rows}
