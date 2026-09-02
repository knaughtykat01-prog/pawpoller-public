"""All SQL CRUD functions for the e621 (E621) analytics database.

Official e621 REST API, HTTP Basic auth. Poll-only, tracks the connected
user's own uploads (tags=user:<username>). Engagement shape:
  - score (score.total, can be negative), favorites_count (fav_count),
    comments_count (comment_count).
  - submission_id is the e621 post id as TEXT.
  - content_type is the file extension family (image / animation / video).

Mirrors database/pix_queries.py; the headline metric column is `score`
instead of `views` (e621 exposes no view count).
"""

from __future__ import annotations
import json
from database.scope import account_clause  # optional `account_id = ?` WHERE-injection
import sqlite3
from datetime import datetime, timezone
from typing import Any


# -- E621 Submissions --------------------------------------------------------

def upsert_e621_submission(conn: sqlite3.Connection, sub: dict, account_id: int) -> None:
    keywords_json = json.dumps(sub.get("keywords", []))
    conn.execute(
        """INSERT INTO e621_submissions
           (submission_id, account_id, title, full_text, username, posted_at, content_type,
            rating, description, keywords, link, thumbnail_url, file_url,
            score, up_score, down_score, favorites_count, comments_count, has_media, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
           ON CONFLICT(submission_id) DO UPDATE SET
            title=excluded.title, full_text=excluded.full_text,
            username=excluded.username, content_type=excluded.content_type,
            rating=excluded.rating, description=excluded.description,
            keywords=excluded.keywords, link=excluded.link,
            thumbnail_url=excluded.thumbnail_url, file_url=excluded.file_url,
            score=excluded.score, up_score=excluded.up_score, down_score=excluded.down_score,
            favorites_count=excluded.favorites_count,
            comments_count=excluded.comments_count,
            has_media=excluded.has_media,
            updated_at=datetime('now')
        """,
        (
            sub["post_uri"], account_id, sub.get("title", ""), sub.get("full_text", ""),
            sub.get("username", ""), sub.get("posted_at"),
            sub.get("content_type", "image"), sub.get("rating", ""),
            sub.get("description", ""), keywords_json,
            sub.get("link", ""), sub.get("thumbnail_url", ""), sub.get("file_url", ""),
            sub.get("score", 0), sub.get("up_score", 0), sub.get("down_score", 0),
            sub.get("favorites_count", 0), sub.get("comments_count", 0),
            sub.get("has_media", 0),
        ),
    )


def get_e621_submission(conn: sqlite3.Connection, submission_id: str) -> dict | None:
    row = conn.execute("SELECT * FROM e621_submissions WHERE submission_id = ?", (submission_id,)).fetchone()
    return dict(row) if row else None


def get_all_e621_submissions(conn: sqlite3.Connection, sort_by: str = "score", order: str = "desc", account_id: int | None = None) -> list[dict]:
    allowed_sorts = {"score", "favorites_count", "comments_count",
                     "title", "posted_at", "updated_at"}
    if sort_by not in allowed_sorts:
        sort_by = "score"
    order_dir = "DESC" if order.lower() == "desc" else "ASC"
    where, params = account_clause(account_id)
    sql = "SELECT * FROM e621_submissions" + (f" WHERE {where}" if where else "")
    sql += f" ORDER BY {sort_by} {order_dir}"
    rows = conn.execute(sql, params).fetchall()
    return [dict(r) for r in rows]


# -- E621 Snapshots ----------------------------------------------------------

def insert_e621_snapshot(conn: sqlite3.Connection, account_id: int, submission_id: str,
                         score: int, favorites_count: int, comments_count: int,
                         polled_at: str | None = None,
                         up_score: int = 0, down_score: int = 0) -> None:
    ts = polled_at or datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    conn.execute(
        "INSERT INTO e621_snapshots (account_id, submission_id, polled_at, score, "
        "up_score, down_score, favorites_count, comments_count) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (account_id, submission_id, ts, score, up_score, down_score,
         favorites_count, comments_count),
    )


def get_e621_snapshots(conn: sqlite3.Connection, submission_id: str,
                       start: str | None = None, end: str | None = None) -> list[dict]:
    sql = "SELECT * FROM e621_snapshots WHERE submission_id = ?"
    params: list[Any] = [submission_id]
    if start:
        sql += " AND polled_at >= ?"
        params.append(start)
    if end:
        sql += " AND polled_at <= ?"
        params.append(end)
    sql += " ORDER BY polled_at ASC"
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def get_e621_aggregate_snapshots(conn: sqlite3.Connection, start: str | None = None,
                                 end: str | None = None, account_id: int | None = None) -> list[dict]:
    sql = ("SELECT polled_at, SUM(score) as score, SUM(favorites_count) as favorites_count, "
           "SUM(comments_count) as comments_count FROM e621_snapshots")
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


def get_e621_comparison_snapshots(conn: sqlite3.Connection, submission_ids: list[str],
                                  start: str | None = None, end: str | None = None) -> dict[str, list[dict]]:
    result: dict[str, list[dict]] = {sid: [] for sid in submission_ids}
    if not submission_ids:
        return result
    placeholders = ",".join("?" * len(submission_ids))
    sql = f"SELECT * FROM e621_snapshots WHERE submission_id IN ({placeholders})"
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


# -- E621 Poll Log -----------------------------------------------------------

def start_e621_poll_log(conn: sqlite3.Connection, account_id: int = 0) -> int:
    cur = conn.execute(
        "INSERT INTO e621_poll_log (started_at, status, account_id) VALUES (datetime('now'), 'running', ?)",
        (account_id,))
    conn.commit()
    return cur.lastrowid


def finish_e621_poll_log(conn: sqlite3.Connection, log_id: int, status: str,
                         submissions_found: int = 0, snapshots_inserted: int = 0,
                         error_message: str | None = None, duration_seconds: float = 0) -> None:
    conn.execute(
        """UPDATE e621_poll_log SET finished_at=datetime('now'), status=?, submissions_found=?,
           snapshots_inserted=?, error_message=?, duration_seconds=?
           WHERE id=?""",
        (status, submissions_found, snapshots_inserted,
         error_message, duration_seconds, log_id),
    )
    conn.commit()


def get_e621_last_poll(conn: sqlite3.Connection) -> dict | None:
    row = conn.execute("SELECT * FROM e621_poll_log ORDER BY started_at DESC LIMIT 1").fetchone()
    return dict(row) if row else None


def get_e621_poll_log(conn: sqlite3.Connection, limit: int = 50) -> list[dict]:
    rows = conn.execute("SELECT * FROM e621_poll_log ORDER BY started_at DESC LIMIT ?", (limit,)).fetchall()
    return [dict(r) for r in rows]


# -- E621 Summary ------------------------------------------------------------

def get_e621_summary(conn: sqlite3.Connection, account_id: int | None = None) -> dict:
    where, wp = account_clause(account_id)
    w = f" WHERE {where}" if where else ""
    totals = conn.execute(
        "SELECT COUNT(*) as total_submissions, COALESCE(SUM(score),0) as total_score, "
        "COALESCE(SUM(favorites_count),0) as total_favorites, "
        "COALESCE(SUM(comments_count),0) as total_comments "
        "FROM e621_submissions" + w,
        wp,
    ).fetchone()
    totals = dict(totals)

    top_scored = conn.execute(
        "SELECT submission_id, title, score FROM e621_submissions" + w + " ORDER BY score DESC LIMIT 5",
        wp,
    ).fetchall()

    top_faved = conn.execute(
        "SELECT submission_id, title, favorites_count FROM e621_submissions" + w + " ORDER BY favorites_count DESC LIMIT 5",
        wp,
    ).fetchall()

    sw, sp = account_clause(account_id, "s")
    fastest_growing = conn.execute(
        """SELECT s.submission_id, s.title,
                  COALESCE(s.score - oldest.score, 0) as score_gained,
                  COALESCE(s.favorites_count - oldest.favorites_count, 0) as favorites_gained
           FROM e621_submissions s
           LEFT JOIN (
               SELECT s1.submission_id, s1.score, s1.favorites_count
               FROM e621_snapshots s1
               INNER JOIN (
                   SELECT submission_id, MAX(polled_at) as max_polled
                   FROM e621_snapshots
                   WHERE polled_at <= datetime('now', '-24 hours')
                   GROUP BY submission_id
               ) s2 ON s1.submission_id = s2.submission_id AND s1.polled_at = s2.max_polled
           ) oldest ON s.submission_id = oldest.submission_id
           WHERE """ + (sw + " AND " if sw else "") + """COALESCE(s.score - oldest.score, 0) > 0
           ORDER BY score_gained DESC LIMIT 5""",
        sp,
    ).fetchall()

    top_scored = [dict(r) for r in top_scored]
    top_faved = [dict(r) for r in top_faved]
    fastest_growing = [dict(r) for r in fastest_growing]
    # e621 posts carry no title, so the stored one is a slice of the
    # description. Prefer the linked Masterpiece's real title where there is one.
    from database import masterpiece_queries as _mq
    _mq.apply_canonical_titles(conn, "e621", top_scored, top_faved, fastest_growing)

    return {
        "total_submissions": totals["total_submissions"],
        "total_score": totals["total_score"],
        "total_favorites": totals["total_favorites"],
        "total_comments": totals["total_comments"],
        "top_scored": top_scored,
        "top_faved": top_faved,
        "fastest_growing": fastest_growing,
    }


# -- E621 Growth Rates -------------------------------------------------------

def _calc_growth_rate(current: int, past: int | None, hours: int) -> float | None:
    if past is None:
        return None
    delta = current - past
    days = hours / 24.0
    return round(delta / days, 2) if days > 0 else None


def get_e621_growth_rates(conn: sqlite3.Connection) -> dict:
    totals = conn.execute(
        "SELECT COALESCE(SUM(score),0) as score, "
        "COALESCE(SUM(favorites_count),0) as favorites_count, "
        "COALESCE(SUM(comments_count),0) as comments_count "
        "FROM e621_submissions"
    ).fetchone()
    current_score = totals["score"]
    current_faves = totals["favorites_count"]
    current_comments = totals["comments_count"]

    rates = {}
    for label, hours in [("24h", 24), ("7d", 168), ("30d", 720)]:
        row = conn.execute(
            """SELECT SUM(score) as score, SUM(favorites_count) as favorites_count,
                      SUM(comments_count) as comments_count
               FROM e621_snapshots WHERE polled_at = (
                   SELECT polled_at FROM e621_snapshots
                   WHERE polled_at <= datetime('now', ? || ' hours')
                   ORDER BY polled_at DESC LIMIT 1
               )""",
            (str(-hours),),
        ).fetchone()
        past_score = row["score"] if row and row["score"] is not None else None
        past_faves = row["favorites_count"] if row and row["favorites_count"] is not None else None
        past_comments = row["comments_count"] if row and row["comments_count"] is not None else None
        rates[label] = {
            "views_per_day": _calc_growth_rate(current_score, past_score, hours),
            "faves_per_day": _calc_growth_rate(current_faves, past_faves, hours),
            "comments_per_day": _calc_growth_rate(current_comments, past_comments, hours),
        }
    return rates


def get_e621_submission_growth_rates(conn: sqlite3.Connection, submission_id: str) -> dict:
    sub = conn.execute(
        "SELECT score, favorites_count, comments_count FROM e621_submissions WHERE submission_id = ?",
        (submission_id,),
    ).fetchone()
    if not sub:
        return {}

    rates = {}
    for label, hours in [("24h", 24), ("7d", 168), ("30d", 720)]:
        row = conn.execute(
            """SELECT score, favorites_count, comments_count
               FROM e621_snapshots WHERE submission_id = ? AND polled_at <= datetime('now', ? || ' hours')
               ORDER BY polled_at DESC LIMIT 1""",
            (submission_id, str(-hours)),
        ).fetchone()
        past_score = row["score"] if row else None
        past_faves = row["favorites_count"] if row else None
        past_comments = row["comments_count"] if row else None
        rates[label] = {
            "views_per_day": _calc_growth_rate(sub["score"], past_score, hours),
            "faves_per_day": _calc_growth_rate(sub["favorites_count"], past_faves, hours),
            "comments_per_day": _calc_growth_rate(sub["comments_count"], past_comments, hours),
        }
    return rates


def get_e621_submission_deltas(conn: sqlite3.Connection) -> dict[str, dict]:
    rows = conn.execute(
        """SELECT s.submission_id,
                  COALESCE(s.score - old.score, 0) as score_delta,
                  COALESCE(s.favorites_count - old.favorites_count, 0) as favorites_delta,
                  COALESCE(s.comments_count - old.comments_count, 0) as comments_delta
           FROM e621_submissions s
           LEFT JOIN (
               SELECT s1.submission_id, s1.score, s1.favorites_count, s1.comments_count
               FROM e621_snapshots s1
               INNER JOIN (
                   SELECT submission_id, MAX(polled_at) as max_polled
                   FROM e621_snapshots
                   WHERE polled_at <= datetime('now', '-24 hours')
                   GROUP BY submission_id
               ) s2 ON s1.submission_id = s2.submission_id AND s1.polled_at = s2.max_polled
           ) old ON s.submission_id = old.submission_id"""
    ).fetchall()
    return {r["submission_id"]: dict(r) for r in rows}
