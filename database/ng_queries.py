"""SQL CRUD for the Newgrounds (`ng`) analytics database — MEDIAPLATS §5 (4.23.0).

Poll+post, scraped. Standard engagement triple — views (Listens / Views) /
favorites_count (Faves) / comments_count — so this mirrors the gallery query
modules (sc/fn/fa/…). `submission_id` is the Newgrounds item id as TEXT; the
row also carries the portal, rating, genre, score, votes and downloads.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from typing import Any

from database.scope import account_clause


# -- Submissions -------------------------------------------------------------

def upsert_ng_submission(conn: sqlite3.Connection, sub: dict, account_id: int) -> None:
    conn.execute(
        """INSERT INTO ng_submissions
           (submission_id, account_id, portal, title, description, username, rating, genre,
            link, thumbnail_url, posted_at,
            views, favorites_count, comments_count, downloads_count, votes, score, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
           ON CONFLICT(submission_id) DO UPDATE SET
            portal=excluded.portal, title=excluded.title, description=excluded.description,
            username=excluded.username, rating=excluded.rating, genre=excluded.genre,
            link=excluded.link, thumbnail_url=excluded.thumbnail_url,
            posted_at=COALESCE(NULLIF(excluded.posted_at, ''), ng_submissions.posted_at),
            views=excluded.views, favorites_count=excluded.favorites_count,
            comments_count=excluded.comments_count, downloads_count=excluded.downloads_count,
            votes=excluded.votes, score=excluded.score, updated_at=datetime('now')
        """,
        (
            sub["submission_id"], account_id, sub.get("portal", "audio"), sub.get("title", ""),
            sub.get("description", ""), sub.get("username", ""), sub.get("rating", ""), sub.get("genre", ""),
            sub.get("link", ""), sub.get("thumbnail_url", ""), sub.get("posted_at") or None,
            sub.get("views", 0), sub.get("favorites_count", 0), sub.get("comments_count", 0),
            sub.get("downloads_count", 0), sub.get("votes", 0), sub.get("score", 0.0),
        ),
    )


def get_ng_submission(conn: sqlite3.Connection, submission_id: str) -> dict | None:
    row = conn.execute("SELECT * FROM ng_submissions WHERE submission_id = ?",
                       (submission_id,)).fetchone()
    return dict(row) if row else None


def get_all_ng_submissions(conn: sqlite3.Connection, sort_by: str = "views",
                           order: str = "desc", account_id: int | None = None) -> list[dict]:
    allowed_sorts = {"views", "favorites_count", "comments_count", "score", "downloads_count",
                     "title", "posted_at", "updated_at"}
    if sort_by not in allowed_sorts:
        sort_by = "views"
    order_dir = "DESC" if order.lower() == "desc" else "ASC"
    where, params = account_clause(account_id)
    sql = "SELECT * FROM ng_submissions" + (f" WHERE {where}" if where else "")
    sql += f" ORDER BY {sort_by} {order_dir}"
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


# -- Snapshots ---------------------------------------------------------------

def insert_ng_snapshot(conn: sqlite3.Connection, account_id: int, submission_id: str,
                       views: int, favorites_count: int, comments_count: int,
                       polled_at: str | None = None, score: float = 0.0, votes: int = 0,
                       downloads_count: int = 0) -> None:
    """The standard triple plus Newgrounds' own score / votes / downloads, so the judgment
    (the public's score) is a series and not only a number."""
    ts = polled_at or datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    conn.execute(
        "INSERT INTO ng_snapshots (account_id, submission_id, polled_at, views, "
        "favorites_count, comments_count, score, votes, downloads_count) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (account_id, submission_id, ts, views, favorites_count, comments_count, score, votes, downloads_count),
    )


def get_ng_snapshots(conn: sqlite3.Connection, submission_id: str,
                     start: str | None = None, end: str | None = None) -> list[dict]:
    sql = "SELECT * FROM ng_snapshots WHERE submission_id = ?"
    params: list[Any] = [submission_id]
    if start:
        sql += " AND polled_at >= ?"
        params.append(start)
    if end:
        sql += " AND polled_at <= ?"
        params.append(end)
    sql += " ORDER BY polled_at ASC"
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def get_ng_aggregate_snapshots(conn: sqlite3.Connection, start: str | None = None,
                               end: str | None = None, account_id: int | None = None) -> list[dict]:
    sql = ("SELECT polled_at, SUM(views) as views, SUM(favorites_count) as favorites_count, "
           "SUM(comments_count) as comments_count FROM ng_snapshots")
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


def get_ng_comparison_snapshots(conn: sqlite3.Connection, submission_ids: list[str],
                                start: str | None = None, end: str | None = None) -> dict[str, list[dict]]:
    result: dict[str, list[dict]] = {sid: [] for sid in submission_ids}
    if not submission_ids:
        return result
    placeholders = ",".join("?" * len(submission_ids))
    sql = f"SELECT * FROM ng_snapshots WHERE submission_id IN ({placeholders})"
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


# -- Poll Log ----------------------------------------------------------------

def start_ng_poll_log(conn: sqlite3.Connection, account_id: int = 0) -> int:
    cur = conn.execute(
        "INSERT INTO ng_poll_log (started_at, status, account_id) "
        "VALUES (datetime('now'), 'running', ?)", (account_id,))
    conn.commit()
    return cur.lastrowid


def finish_ng_poll_log(conn: sqlite3.Connection, log_id: int, status: str,
                       submissions_found: int = 0, snapshots_inserted: int = 0,
                       error_message: str | None = None, duration_seconds: float = 0) -> None:
    conn.execute(
        """UPDATE ng_poll_log SET finished_at=datetime('now'), status=?, submissions_found=?,
           snapshots_inserted=?, error_message=?, duration_seconds=? WHERE id=?""",
        (status, submissions_found, snapshots_inserted, error_message, duration_seconds, log_id),
    )
    conn.commit()


def get_ng_last_poll(conn: sqlite3.Connection) -> dict | None:
    row = conn.execute("SELECT * FROM ng_poll_log ORDER BY started_at DESC LIMIT 1").fetchone()
    return dict(row) if row else None


def get_ng_poll_log(conn: sqlite3.Connection, limit: int = 50) -> list[dict]:
    rows = conn.execute("SELECT * FROM ng_poll_log ORDER BY started_at DESC LIMIT ?", (limit,)).fetchall()
    return [dict(r) for r in rows]


# -- Summary -----------------------------------------------------------------

def get_ng_summary(conn: sqlite3.Connection, account_id: int | None = None) -> dict:
    where, wp = account_clause(account_id)
    w = f" WHERE {where}" if where else ""
    totals = dict(conn.execute(
        "SELECT COUNT(*) as total_submissions, COALESCE(SUM(views),0) as total_views, "
        "COALESCE(SUM(favorites_count),0) as total_favorites, "
        "COALESCE(SUM(comments_count),0) as total_comments FROM ng_submissions" + w, wp,
    ).fetchone())

    top_viewed = conn.execute(
        "SELECT submission_id, title, views FROM ng_submissions" + w + " ORDER BY views DESC LIMIT 5", wp,
    ).fetchall()
    top_faved = conn.execute(
        "SELECT submission_id, title, favorites_count FROM ng_submissions" + w
        + " ORDER BY favorites_count DESC LIMIT 5", wp,
    ).fetchall()

    sw, sp = account_clause(account_id, "s")
    fastest_growing = conn.execute(
        """SELECT s.submission_id, s.title,
                  COALESCE(s.views - oldest.views, 0) as views_gained,
                  COALESCE(s.favorites_count - oldest.favorites_count, 0) as favorites_gained
           FROM ng_submissions s
           LEFT JOIN (
               SELECT s1.submission_id, s1.views, s1.favorites_count
               FROM ng_snapshots s1
               INNER JOIN (
                   SELECT submission_id, MAX(polled_at) as max_polled
                   FROM ng_snapshots
                   WHERE polled_at <= datetime('now', '-24 hours')
                   GROUP BY submission_id
               ) s2 ON s1.submission_id = s2.submission_id AND s1.polled_at = s2.max_polled
           ) oldest ON s.submission_id = oldest.submission_id
           WHERE """ + (sw + " AND " if sw else "") + """COALESCE(s.views - oldest.views, 0) > 0
           ORDER BY views_gained DESC LIMIT 5""", sp,
    ).fetchall()

    return {
        "total_submissions": totals["total_submissions"],
        "total_views": totals["total_views"],
        "total_favorites": totals["total_favorites"],
        "total_comments": totals["total_comments"],
        "top_viewed": [dict(r) for r in top_viewed],
        "top_faved": [dict(r) for r in top_faved],
        "fastest_growing": [dict(r) for r in fastest_growing],
    }


# -- Growth Rates ------------------------------------------------------------

def _calc_growth_rate(current: int, past: int | None, hours: int) -> float | None:
    if past is None:
        return None
    days = hours / 24.0
    return round((current - past) / days, 2) if days > 0 else None


def get_ng_growth_rates(conn: sqlite3.Connection) -> dict:
    totals = conn.execute(
        "SELECT COALESCE(SUM(views),0) as views, COALESCE(SUM(favorites_count),0) as favorites_count, "
        "COALESCE(SUM(comments_count),0) as comments_count FROM ng_submissions"
    ).fetchone()
    rates = {}
    for label, hours in [("24h", 24), ("7d", 168), ("30d", 720)]:
        row = conn.execute(
            """SELECT SUM(views) as views, SUM(favorites_count) as favorites_count,
                      SUM(comments_count) as comments_count
               FROM ng_snapshots WHERE polled_at = (
                   SELECT polled_at FROM ng_snapshots
                   WHERE polled_at <= datetime('now', ? || ' hours')
                   ORDER BY polled_at DESC LIMIT 1)""",
            (str(-hours),),
        ).fetchone()
        rates[label] = {
            "views_per_day": _calc_growth_rate(totals["views"], row["views"] if row else None, hours),
            "faves_per_day": _calc_growth_rate(totals["favorites_count"], row["favorites_count"] if row else None, hours),
            "comments_per_day": _calc_growth_rate(totals["comments_count"], row["comments_count"] if row else None, hours),
        }
    return rates


def get_ng_submission_growth_rates(conn: sqlite3.Connection, submission_id: str) -> dict:
    sub = conn.execute(
        "SELECT views, favorites_count, comments_count FROM ng_submissions WHERE submission_id = ?",
        (submission_id,),
    ).fetchone()
    if not sub:
        return {}
    rates = {}
    for label, hours in [("24h", 24), ("7d", 168), ("30d", 720)]:
        row = conn.execute(
            """SELECT views, favorites_count, comments_count FROM ng_snapshots
               WHERE submission_id = ? AND polled_at <= datetime('now', ? || ' hours')
               ORDER BY polled_at DESC LIMIT 1""",
            (submission_id, str(-hours)),
        ).fetchone()
        rates[label] = {
            "views_per_day": _calc_growth_rate(sub["views"], row["views"] if row else None, hours),
            "faves_per_day": _calc_growth_rate(sub["favorites_count"], row["favorites_count"] if row else None, hours),
            "comments_per_day": _calc_growth_rate(sub["comments_count"], row["comments_count"] if row else None, hours),
        }
    return rates


def get_ng_submission_deltas(conn: sqlite3.Connection) -> dict[str, dict]:
    rows = conn.execute(
        """SELECT s.submission_id,
                  COALESCE(s.views - old.views, 0) as views_delta,
                  COALESCE(s.favorites_count - old.favorites_count, 0) as favorites_delta,
                  COALESCE(s.comments_count - old.comments_count, 0) as comments_delta
           FROM ng_submissions s
           LEFT JOIN (
               SELECT s1.submission_id, s1.views, s1.favorites_count, s1.comments_count
               FROM ng_snapshots s1
               INNER JOIN (
                   SELECT submission_id, MAX(polled_at) as max_polled
                   FROM ng_snapshots
                   WHERE polled_at <= datetime('now', '-24 hours')
                   GROUP BY submission_id
               ) s2 ON s1.submission_id = s2.submission_id AND s1.polled_at = s2.max_polled
           ) old ON s.submission_id = old.submission_id"""
    ).fetchall()
    return {r["submission_id"]: dict(r) for r in rows}
