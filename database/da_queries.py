"""All SQL CRUD functions for the DeviantArt (DA) analytics database.

DeviantArt uses Eclipse internal _napi endpoints with cookie-based auth.
Compared to other platforms in PawPoller, DA has an additional metric: downloads.

Key differences from other platforms:
  - submission_id is INTEGER (DeviantArt deviation IDs)
  - Has downloads in addition to views, favorites_count, comments_count
  - No individual comment tracking (just count in snapshots)
  - No faving_users or watchers tables
  - Cookie-based authentication (full cookie string from browser)
"""

from __future__ import annotations
import json
from database.scope import account_clause  # optional `account_id = ?` WHERE-injection
import sqlite3
from datetime import datetime, timezone
from typing import Any


# -- DA Submissions ---------------------------------------------------

def upsert_da_submission(conn: sqlite3.Connection, sub: dict, account_id: int) -> None:
    """Insert or update a DA deviation's metadata and latest stats."""
    keywords_json = json.dumps(sub.get("keywords", []))
    # account_id set on INSERT only; the ON CONFLICT UPDATE leaves it alone.
    conn.execute(
        """INSERT INTO da_submissions
           (submission_id, account_id, title, username, posted_at, category, rating,
            description, keywords, link, thumbnail_url,
            views, favorites_count, comments_count, downloads, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
           ON CONFLICT(submission_id) DO UPDATE SET
            title=excluded.title, username=excluded.username,
            category=excluded.category, rating=excluded.rating,
            description=excluded.description, keywords=excluded.keywords,
            link=excluded.link, thumbnail_url=excluded.thumbnail_url,
            views=excluded.views, favorites_count=excluded.favorites_count,
            comments_count=excluded.comments_count, downloads=excluded.downloads,
            updated_at=datetime('now')
        """,
        (
            sub["deviation_id"], account_id, sub.get("title", ""), sub.get("username", ""),
            sub.get("posted_at"), sub.get("category", ""),
            sub.get("rating", ""), sub.get("description", ""),
            keywords_json, sub.get("link", ""),
            sub.get("thumbnail_url", ""),
            sub.get("views", 0), sub.get("favorites_count", 0),
            sub.get("comments_count", 0), sub.get("downloads", 0),
        ),
    )


def get_da_submission(conn: sqlite3.Connection, submission_id: int) -> dict | None:
    row = conn.execute("SELECT * FROM da_submissions WHERE submission_id = ?", (submission_id,)).fetchone()
    return dict(row) if row else None


def get_all_da_submissions(conn: sqlite3.Connection, sort_by: str = "views", order: str = "desc", account_id: int | None = None) -> list[dict]:
    allowed_sorts = {"views", "favorites_count", "comments_count", "downloads",
                     "title", "posted_at", "updated_at"}
    if sort_by not in allowed_sorts:
        sort_by = "views"
    order_dir = "DESC" if order.lower() == "desc" else "ASC"
    where, params = account_clause(account_id)
    sql = "SELECT * FROM da_submissions" + (f" WHERE {where}" if where else "")
    sql += f" ORDER BY {sort_by} {order_dir}"
    rows = conn.execute(sql, params).fetchall()
    return [dict(r) for r in rows]


# -- DA Snapshots -----------------------------------------------------

def insert_da_snapshot(conn: sqlite3.Connection, account_id: int, submission_id: int, views: int,
                       favorites_count: int, comments_count: int, downloads: int,
                       polled_at: str | None = None) -> None:
    ts = polled_at or datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    conn.execute(
        "INSERT INTO da_snapshots (account_id, submission_id, polled_at, views, favorites_count, comments_count, downloads) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (account_id, submission_id, ts, views, favorites_count, comments_count, downloads),
    )


def get_da_snapshots(conn: sqlite3.Connection, submission_id: int,
                     start: str | None = None, end: str | None = None) -> list[dict]:
    sql = "SELECT * FROM da_snapshots WHERE submission_id = ?"
    params: list[Any] = [submission_id]
    if start:
        sql += " AND polled_at >= ?"
        params.append(start)
    if end:
        sql += " AND polled_at <= ?"
        params.append(end)
    sql += " ORDER BY polled_at ASC"
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def get_da_aggregate_snapshots(conn: sqlite3.Connection, start: str | None = None,
                               end: str | None = None, account_id: int | None = None) -> list[dict]:
    sql = ("SELECT polled_at, SUM(views) as views, SUM(favorites_count) as favorites_count, "
           "SUM(comments_count) as comments_count, SUM(downloads) as downloads "
           "FROM da_snapshots")
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


def get_da_comparison_snapshots(conn: sqlite3.Connection, submission_ids: list[int],
                                start: str | None = None, end: str | None = None) -> dict[str, list[dict]]:
    """Multi-submission time-series. One IN-clause query instead of N SELECTs."""
    result: dict[str, list[dict]] = {str(sid): [] for sid in submission_ids}
    if not submission_ids:
        return result
    placeholders = ",".join("?" * len(submission_ids))
    sql = f"SELECT * FROM da_snapshots WHERE submission_id IN ({placeholders})"
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


# -- DA Poll Log ------------------------------------------------------

def start_da_poll_log(conn: sqlite3.Connection, account_id: int = 0) -> int:
    cur = conn.execute(
        "INSERT INTO da_poll_log (started_at, status, account_id) VALUES (datetime('now'), 'running', ?)",
        (account_id,))
    conn.commit()
    return cur.lastrowid


def finish_da_poll_log(conn: sqlite3.Connection, log_id: int, status: str,
                       submissions_found: int = 0, snapshots_inserted: int = 0,
                       error_message: str | None = None, duration_seconds: float = 0) -> None:
    conn.execute(
        """UPDATE da_poll_log SET finished_at=datetime('now'), status=?, submissions_found=?,
           snapshots_inserted=?, error_message=?, duration_seconds=?
           WHERE id=?""",
        (status, submissions_found, snapshots_inserted,
         error_message, duration_seconds, log_id),
    )
    conn.commit()


def get_da_last_poll(conn: sqlite3.Connection) -> dict | None:
    row = conn.execute("SELECT * FROM da_poll_log ORDER BY started_at DESC LIMIT 1").fetchone()
    return dict(row) if row else None


def get_da_poll_log(conn: sqlite3.Connection, limit: int = 50) -> list[dict]:
    rows = conn.execute("SELECT * FROM da_poll_log ORDER BY started_at DESC LIMIT ?", (limit,)).fetchall()
    return [dict(r) for r in rows]


# -- DA Summary -------------------------------------------------------

def get_da_summary(conn: sqlite3.Connection, account_id: int | None = None) -> dict:
    """Main DA dashboard data source: totals + top-lists + fastest growing.

    With *account_id* set, every total/top-list is scoped to that account; the
    "All accounts" default (account_id=None) keeps the aggregate behaviour.
    DA has no stat offsets and no individual fave/comment tracking, so there is
    nothing else to scope here.
    """
    where, wp = account_clause(account_id)
    w = f" WHERE {where}" if where else ""
    totals = conn.execute(
        "SELECT COUNT(*) as total_submissions, COALESCE(SUM(views),0) as total_views, "
        "COALESCE(SUM(favorites_count),0) as total_favorites, "
        "COALESCE(SUM(comments_count),0) as total_comments, "
        "COALESCE(SUM(downloads),0) as total_downloads "
        "FROM da_submissions" + w,
        wp,
    ).fetchone()
    totals = dict(totals)

    top_viewed = conn.execute(
        "SELECT submission_id, title, views, thumbnail_url as thumb_url FROM da_submissions" + w + " ORDER BY views DESC LIMIT 5",
        wp,
    ).fetchall()

    top_faved = conn.execute(
        "SELECT submission_id, title, favorites_count, thumbnail_url as thumb_url FROM da_submissions" + w + " ORDER BY favorites_count DESC LIMIT 5",
        wp,
    ).fetchall()

    top_downloaded = conn.execute(
        "SELECT submission_id, title, downloads, thumbnail_url as thumb_url FROM da_submissions" + w + " ORDER BY downloads DESC LIMIT 5",
        wp,
    ).fetchall()

    # Fastest-growing: only the outer `s` (da_submissions) needs account scoping —
    # submission_ids are unique to their account, so the snapshot join is
    # implicitly account-correct.
    sw, sp = account_clause(account_id, "s")
    fastest_growing = conn.execute(
        """SELECT s.submission_id, s.title, s.thumbnail_url as thumb_url,
                  COALESCE(s.views - oldest.views, 0) as views_gained,
                  COALESCE(s.favorites_count - oldest.favorites_count, 0) as faves_gained
           FROM da_submissions s
           LEFT JOIN (
               SELECT s1.submission_id, s1.views, s1.favorites_count
               FROM da_snapshots s1
               INNER JOIN (
                   SELECT submission_id, MAX(polled_at) as max_polled
                   FROM da_snapshots
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
        "total_favorites": totals["total_favorites"],
        "total_comments": totals["total_comments"],
        "total_downloads": totals["total_downloads"],
        "top_viewed": [dict(r) for r in top_viewed],
        "top_faved": [dict(r) for r in top_faved],
        "top_downloaded": [dict(r) for r in top_downloaded],
        "fastest_growing": [dict(r) for r in fastest_growing],
    }


# -- DA Growth Rates --------------------------------------------------

def _calc_growth_rate(current: int, past: int | None, hours: int) -> float | None:
    if past is None:
        return None
    delta = current - past
    days = hours / 24.0
    return round(delta / days, 2) if days > 0 else None


def get_da_growth_rates(conn: sqlite3.Connection) -> dict:
    totals = conn.execute(
        "SELECT COALESCE(SUM(views),0) as views, COALESCE(SUM(favorites_count),0) as faves, "
        "COALESCE(SUM(comments_count),0) as comments, COALESCE(SUM(downloads),0) as downloads "
        "FROM da_submissions"
    ).fetchone()
    current_views = totals["views"]
    current_faves = totals["faves"]
    current_comments = totals["comments"]
    current_downloads = totals["downloads"]

    rates = {}
    for label, hours in [("24h", 24), ("7d", 168), ("30d", 720)]:
        row = conn.execute(
            """SELECT SUM(views) as views, SUM(favorites_count) as faves,
                      SUM(comments_count) as comments, SUM(downloads) as downloads
               FROM da_snapshots WHERE polled_at = (
                   SELECT polled_at FROM da_snapshots
                   WHERE polled_at <= datetime('now', ? || ' hours')
                   ORDER BY polled_at DESC LIMIT 1
               )""",
            (str(-hours),),
        ).fetchone()
        past_views = row["views"] if row and row["views"] is not None else None
        past_faves = row["faves"] if row and row["faves"] is not None else None
        past_comments = row["comments"] if row and row["comments"] is not None else None
        past_downloads = row["downloads"] if row and row["downloads"] is not None else None
        rates[label] = {
            "views_per_day": _calc_growth_rate(current_views, past_views, hours),
            "faves_per_day": _calc_growth_rate(current_faves, past_faves, hours),
            "comments_per_day": _calc_growth_rate(current_comments, past_comments, hours),
            "downloads_per_day": _calc_growth_rate(current_downloads, past_downloads, hours),
        }
    return rates


def get_da_submission_growth_rates(conn: sqlite3.Connection, submission_id: int) -> dict:
    sub = conn.execute(
        "SELECT views, favorites_count, comments_count, downloads FROM da_submissions WHERE submission_id = ?",
        (submission_id,),
    ).fetchone()
    if not sub:
        return {}

    rates = {}
    for label, hours in [("24h", 24), ("7d", 168), ("30d", 720)]:
        row = conn.execute(
            """SELECT views, favorites_count as faves, comments_count as comments, downloads
               FROM da_snapshots WHERE submission_id = ? AND polled_at <= datetime('now', ? || ' hours')
               ORDER BY polled_at DESC LIMIT 1""",
            (submission_id, str(-hours)),
        ).fetchone()
        past_views = row["views"] if row else None
        past_faves = row["faves"] if row else None
        past_comments = row["comments"] if row else None
        past_downloads = row["downloads"] if row else None
        rates[label] = {
            "views_per_day": _calc_growth_rate(sub["views"], past_views, hours),
            "faves_per_day": _calc_growth_rate(sub["favorites_count"], past_faves, hours),
            "comments_per_day": _calc_growth_rate(sub["comments_count"], past_comments, hours),
            "downloads_per_day": _calc_growth_rate(sub["downloads"], past_downloads, hours),
        }
    return rates


def get_da_submission_deltas(conn: sqlite3.Connection) -> dict[str, dict]:
    rows = conn.execute(
        """SELECT s.submission_id,
                  COALESCE(s.views - old.views, 0) as views_delta,
                  COALESCE(s.favorites_count - old.favorites_count, 0) as faves_delta,
                  COALESCE(s.comments_count - old.comments_count, 0) as comments_delta,
                  COALESCE(s.downloads - old.downloads, 0) as downloads_delta
           FROM da_submissions s
           LEFT JOIN (
               SELECT s1.submission_id, s1.views, s1.favorites_count, s1.comments_count, s1.downloads
               FROM da_snapshots s1
               INNER JOIN (
                   SELECT submission_id, MAX(polled_at) as max_polled
                   FROM da_snapshots
                   WHERE polled_at <= datetime('now', '-24 hours')
                   GROUP BY submission_id
               ) s2 ON s1.submission_id = s2.submission_id AND s1.polled_at = s2.max_polled
           ) old ON s.submission_id = old.submission_id"""
    ).fetchall()
    return {str(r["submission_id"]): dict(r) for r in rows}
