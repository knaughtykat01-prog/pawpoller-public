"""All SQL CRUD functions for the Itaku (IK) analytics database.

Itaku provides a public REST API at itaku.ee/api/. No authentication
is required — only a target username is needed to discover and track content.

Key differences from other platforms:
  - submission_id is INTEGER (Itaku content IDs)
  - Uses likes instead of views/favorites, reshares as unique metric
  - NO views metric available on Itaku
  - Tracks content_type (image or post) and thumbnail_url
  - No kudos/fave/comment individual user tracking — just counts
"""

from __future__ import annotations
import json
from database.scope import account_clause  # optional `account_id = ?` WHERE-injection
import sqlite3
from datetime import datetime, timezone
from typing import Any


# -- IK Submissions -------------------------------------------------------

def upsert_ik_submission(conn: sqlite3.Connection, sub: dict, account_id: int) -> None:
    """Insert or update an Itaku content item's metadata and latest stats."""
    keywords_json = json.dumps(sub.get("keywords", []))

    # account_id set on INSERT only; the ON CONFLICT UPDATE leaves it alone.
    conn.execute(
        """INSERT INTO ik_submissions
           (submission_id, account_id, title, username, posted_at, content_type, rating,
            description, keywords, link, thumbnail_url,
            likes, comments_count, reshares, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
           ON CONFLICT(submission_id) DO UPDATE SET
            title=excluded.title, username=excluded.username,
            content_type=excluded.content_type, rating=excluded.rating,
            description=excluded.description, keywords=excluded.keywords,
            link=excluded.link, thumbnail_url=excluded.thumbnail_url,
            likes=excluded.likes, comments_count=excluded.comments_count,
            reshares=excluded.reshares, updated_at=datetime('now')
        """,
        (
            sub["content_id"], account_id, sub.get("title", ""), sub.get("username", ""),
            sub.get("posted_at"), sub.get("content_type", "image"),
            sub.get("rating", ""), sub.get("description", ""),
            keywords_json, sub.get("link", ""), sub.get("thumbnail_url", ""),
            sub.get("likes", 0), sub.get("comments_count", 0),
            sub.get("reshares", 0),
        ),
    )


def get_ik_submission(conn: sqlite3.Connection, submission_id: int) -> dict | None:
    row = conn.execute("SELECT * FROM ik_submissions WHERE submission_id = ?", (submission_id,)).fetchone()
    return dict(row) if row else None


def get_ik_previous_comments_count(conn: sqlite3.Connection, submission_id: int) -> int | None:
    row = conn.execute(
        "SELECT comments_count FROM ik_snapshots WHERE submission_id = ? ORDER BY polled_at DESC LIMIT 1",
        (submission_id,),
    ).fetchone()
    return row["comments_count"] if row else None


def get_all_ik_submissions(conn: sqlite3.Connection, sort_by: str = "likes", order: str = "desc", account_id: int | None = None) -> list[dict]:
    allowed_sorts = {"likes", "comments_count", "reshares",
                     "title", "posted_at", "updated_at", "content_type"}
    if sort_by not in allowed_sorts:
        sort_by = "likes"
    order_dir = "DESC" if order.lower() == "desc" else "ASC"
    where, params = account_clause(account_id)
    sql = "SELECT * FROM ik_submissions" + (f" WHERE {where}" if where else "")
    sql += f" ORDER BY {sort_by} {order_dir}"
    rows = conn.execute(sql, params).fetchall()
    return [dict(r) for r in rows]


# -- IK Snapshots ---------------------------------------------------------

def insert_ik_snapshot(conn: sqlite3.Connection, account_id: int, submission_id: int, likes: int,
                       comments_count: int, reshares: int,
                       polled_at: str | None = None) -> None:
    ts = polled_at or datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    conn.execute(
        "INSERT INTO ik_snapshots (account_id, submission_id, polled_at, likes, comments_count, reshares) VALUES (?, ?, ?, ?, ?, ?)",
        (account_id, submission_id, ts, likes, comments_count, reshares),
    )


def get_ik_snapshots(conn: sqlite3.Connection, submission_id: int,
                     start: str | None = None, end: str | None = None) -> list[dict]:
    sql = "SELECT * FROM ik_snapshots WHERE submission_id = ?"
    params: list[Any] = [submission_id]
    if start:
        sql += " AND polled_at >= ?"
        params.append(start)
    if end:
        sql += " AND polled_at <= ?"
        params.append(end)
    sql += " ORDER BY polled_at ASC"
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def get_ik_aggregate_snapshots(conn: sqlite3.Connection, start: str | None = None,
                               end: str | None = None, account_id: int | None = None) -> list[dict]:
    sql = ("SELECT polled_at, SUM(likes) as likes, "
           "SUM(comments_count) as comments_count, SUM(reshares) as reshares "
           "FROM ik_snapshots")
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


def get_ik_comparison_snapshots(conn: sqlite3.Connection, submission_ids: list[int],
                                start: str | None = None, end: str | None = None) -> dict[str, list[dict]]:
    """Multi-submission time-series. One IN-clause query instead of N SELECTs."""
    result: dict[str, list[dict]] = {str(sid): [] for sid in submission_ids}
    if not submission_ids:
        return result
    placeholders = ",".join("?" * len(submission_ids))
    sql = f"SELECT * FROM ik_snapshots WHERE submission_id IN ({placeholders})"
    params: list[Any] = list(submission_ids)
    if start:
        sql += " AND polled_at >= ?"
        params.append(start)
    if end:
        sql += " AND polled_at <= ?"
        params.append(end)
    sql += " ORDER BY submission_id, polled_at ASC"
    for row in conn.execute(sql, params).fetchall():
        result[str(row["submission_id"])].append(dict(row))
    return result


# -- IK Poll Log ----------------------------------------------------------

def start_ik_poll_log(conn: sqlite3.Connection, account_id: int = 0) -> int:
    cur = conn.execute(
        "INSERT INTO ik_poll_log (started_at, status, account_id) VALUES (datetime('now'), 'running', ?)",
        (account_id,))
    conn.commit()
    return cur.lastrowid


def finish_ik_poll_log(conn: sqlite3.Connection, log_id: int, status: str,
                       submissions_found: int = 0, snapshots_inserted: int = 0,
                       error_message: str | None = None, duration_seconds: float = 0) -> None:
    conn.execute(
        """UPDATE ik_poll_log SET finished_at=datetime('now'), status=?, submissions_found=?,
           snapshots_inserted=?, error_message=?, duration_seconds=?
           WHERE id=?""",
        (status, submissions_found, snapshots_inserted,
         error_message, duration_seconds, log_id),
    )
    conn.commit()


def get_ik_last_poll(conn: sqlite3.Connection) -> dict | None:
    row = conn.execute("SELECT * FROM ik_poll_log ORDER BY started_at DESC LIMIT 1").fetchone()
    return dict(row) if row else None


def get_ik_poll_log(conn: sqlite3.Connection, limit: int = 50) -> list[dict]:
    rows = conn.execute("SELECT * FROM ik_poll_log ORDER BY started_at DESC LIMIT ?", (limit,)).fetchall()
    return [dict(r) for r in rows]


# -- IK Summary -----------------------------------------------------------

def get_ik_summary(conn: sqlite3.Connection, account_id: int | None = None) -> dict:
    # With *account_id* set, every total/top-list is scoped to that account; the
    # "All accounts" default (account_id=None) keeps the aggregate behaviour.
    # Itaku has no stat offsets and no individual fave/comment rows, so there is
    # nothing to guard with `if account_id is None` and no recent-activity calls.
    where, wp = account_clause(account_id)
    w = f" WHERE {where}" if where else ""
    totals = conn.execute(
        "SELECT COUNT(*) as total_submissions, COALESCE(SUM(likes),0) as total_likes, "
        "COALESCE(SUM(comments_count),0) as total_comments, "
        "COALESCE(SUM(reshares),0) as total_reshares "
        "FROM ik_submissions" + w,
        wp,
    ).fetchone()
    totals = dict(totals)

    top_liked = conn.execute(
        "SELECT submission_id, title, likes FROM ik_submissions" + w + " ORDER BY likes DESC LIMIT 5",
        wp,
    ).fetchall()

    top_reshared = conn.execute(
        "SELECT submission_id, title, reshares FROM ik_submissions" + w + " ORDER BY reshares DESC LIMIT 5",
        wp,
    ).fetchall()

    # Fastest-growing: only the outer `s` needs account scoping — submission_ids
    # are unique to their account, so the snapshot join is implicitly correct.
    sw, sp = account_clause(account_id, "s")
    fastest_growing = conn.execute(
        """SELECT s.submission_id, s.title,
                  COALESCE(s.likes - oldest.likes, 0) as likes_gained,
                  COALESCE(s.reshares - oldest.reshares, 0) as reshares_gained
           FROM ik_submissions s
           LEFT JOIN (
               SELECT s1.submission_id, s1.likes, s1.reshares
               FROM ik_snapshots s1
               INNER JOIN (
                   SELECT submission_id, MAX(polled_at) as max_polled
                   FROM ik_snapshots
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
        "total_comments": totals["total_comments"],
        "total_reshares": totals["total_reshares"],
        "top_liked": [dict(r) for r in top_liked],
        "top_reshared": [dict(r) for r in top_reshared],
        "fastest_growing": [dict(r) for r in fastest_growing],
    }


# -- IK Growth Rates ------------------------------------------------------

def _calc_growth_rate(current: int, past: int | None, hours: int) -> float | None:
    if past is None:
        return None
    delta = current - past
    days = hours / 24.0
    return round(delta / days, 2) if days > 0 else None


def get_ik_growth_rates(conn: sqlite3.Connection) -> dict:
    totals = conn.execute(
        "SELECT COALESCE(SUM(likes),0) as likes, "
        "COALESCE(SUM(comments_count),0) as comments, "
        "COALESCE(SUM(reshares),0) as reshares "
        "FROM ik_submissions"
    ).fetchone()
    current_likes = totals["likes"]
    current_comments = totals["comments"]
    current_reshares = totals["reshares"]

    rates = {}
    for label, hours in [("24h", 24), ("7d", 168), ("30d", 720)]:
        row = conn.execute(
            """SELECT SUM(likes) as likes,
                      SUM(comments_count) as comments, SUM(reshares) as reshares
               FROM ik_snapshots WHERE polled_at = (
                   SELECT polled_at FROM ik_snapshots
                   WHERE polled_at <= datetime('now', ? || ' hours')
                   ORDER BY polled_at DESC LIMIT 1
               )""",
            (str(-hours),),
        ).fetchone()
        past_likes = row["likes"] if row and row["likes"] is not None else None
        past_comments = row["comments"] if row and row["comments"] is not None else None
        past_reshares = row["reshares"] if row and row["reshares"] is not None else None
        rates[label] = {
            "likes_per_day": _calc_growth_rate(current_likes, past_likes, hours),
            "comments_per_day": _calc_growth_rate(current_comments, past_comments, hours),
            "reshares_per_day": _calc_growth_rate(current_reshares, past_reshares, hours),
        }
    return rates


def get_ik_submission_growth_rates(conn: sqlite3.Connection, submission_id: int) -> dict:
    sub = conn.execute(
        "SELECT likes, comments_count, reshares FROM ik_submissions WHERE submission_id = ?",
        (submission_id,),
    ).fetchone()
    if not sub:
        return {}

    rates = {}
    for label, hours in [("24h", 24), ("7d", 168), ("30d", 720)]:
        row = conn.execute(
            """SELECT likes, comments_count as comments, reshares
               FROM ik_snapshots WHERE submission_id = ? AND polled_at <= datetime('now', ? || ' hours')
               ORDER BY polled_at DESC LIMIT 1""",
            (submission_id, str(-hours)),
        ).fetchone()
        past_likes = row["likes"] if row else None
        past_comments = row["comments"] if row else None
        past_reshares = row["reshares"] if row else None
        rates[label] = {
            "likes_per_day": _calc_growth_rate(sub["likes"], past_likes, hours),
            "comments_per_day": _calc_growth_rate(sub["comments_count"], past_comments, hours),
            "reshares_per_day": _calc_growth_rate(sub["reshares"], past_reshares, hours),
        }
    return rates


def get_ik_submission_deltas(conn: sqlite3.Connection) -> dict[str, dict]:
    rows = conn.execute(
        """SELECT s.submission_id,
                  COALESCE(s.likes - old.likes, 0) as likes_delta,
                  COALESCE(s.comments_count - old.comments_count, 0) as comments_delta,
                  COALESCE(s.reshares - old.reshares, 0) as reshares_delta
           FROM ik_submissions s
           LEFT JOIN (
               SELECT s1.submission_id, s1.likes, s1.comments_count, s1.reshares
               FROM ik_snapshots s1
               INNER JOIN (
                   SELECT submission_id, MAX(polled_at) as max_polled
                   FROM ik_snapshots
                   WHERE polled_at <= datetime('now', '-24 hours')
                   GROUP BY submission_id
               ) s2 ON s1.submission_id = s2.submission_id AND s1.polled_at = s2.max_polled
           ) old ON s.submission_id = old.submission_id"""
    ).fetchall()
    return {str(r["submission_id"]): dict(r) for r in rows}
