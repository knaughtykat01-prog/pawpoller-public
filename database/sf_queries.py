"""All SQL CRUD functions for the SoFurry (SF) analytics database.

Mirrors the structure of ws_queries.py (Weasyl) since SoFurry has similar
data availability: views, likes (favorites_count), and comment counts only.

Key differences from other platforms:
  - submission_id is TEXT (alphanumeric slug like "nZ7RvxM1"), not INTEGER
  - No individual comment tracking (count only, like Weasyl)
  - No faving-user tracking (count only)
  - content_type instead of type_name/subtype
"""

from __future__ import annotations
import json
import sqlite3
from datetime import datetime, timezone
from typing import Any

from database.scope import account_clause  # optional `account_id = ?` WHERE-injection


# -- SF Submissions ----------------------------------------------------

def upsert_sf_submission(conn: sqlite3.Connection, sub: dict, account_id: int) -> None:
    """Insert or update a SoFurry submission's metadata and latest stats."""
    keywords_json = json.dumps(sub.get("keywords", []))
    # account_id set on INSERT only; the ON CONFLICT UPDATE leaves it alone.
    conn.execute(
        """INSERT INTO sf_submissions
           (submission_id, account_id, title, username, posted_at, content_type,
            rating, thumbnail_url, description, keywords, link,
            views, favorites_count, comments_count, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
           ON CONFLICT(submission_id) DO UPDATE SET
            title=excluded.title, username=excluded.username,
            content_type=excluded.content_type,
            rating=excluded.rating, thumbnail_url=excluded.thumbnail_url,
            description=excluded.description, keywords=excluded.keywords,
            link=excluded.link, views=excluded.views,
            favorites_count=excluded.favorites_count,
            comments_count=excluded.comments_count, updated_at=datetime('now')
        """,
        (
            sub["submission_id"], account_id, sub.get("title", ""), sub.get("username", ""),
            sub.get("posted_at"), sub.get("content_type", ""),
            sub.get("rating", ""), sub.get("thumbnail_url", ""),
            sub.get("description", ""), keywords_json, sub.get("link", ""),
            sub.get("views", 0), sub.get("favorites_count", 0),
            sub.get("comments_count", 0),
        ),
    )


def get_sf_submission(conn: sqlite3.Connection, submission_id: str) -> dict | None:
    row = conn.execute("SELECT * FROM sf_submissions WHERE submission_id = ?", (submission_id,)).fetchone()
    return dict(row) if row else None


def get_sf_previous_comments_count(conn: sqlite3.Connection, submission_id: str) -> int | None:
    """Get the comments_count from the most recent SF snapshot."""
    row = conn.execute(
        "SELECT comments_count FROM sf_snapshots WHERE submission_id = ? ORDER BY polled_at DESC LIMIT 1",
        (submission_id,),
    ).fetchone()
    return row["comments_count"] if row else None


def get_all_sf_submissions(conn: sqlite3.Connection, sort_by: str = "views", order: str = "desc", account_id: int | None = None) -> list[dict]:
    allowed_sorts = {"views", "favorites_count", "comments_count", "title", "posted_at", "updated_at"}
    if sort_by not in allowed_sorts:
        sort_by = "views"
    order_dir = "DESC" if order.lower() == "desc" else "ASC"
    where, params = account_clause(account_id)
    sql = "SELECT * FROM sf_submissions" + (f" WHERE {where}" if where else "")
    sql += f" ORDER BY {sort_by} {order_dir}"
    rows = conn.execute(sql, params).fetchall()
    return [dict(r) for r in rows]


# -- SF Snapshots ------------------------------------------------------

def insert_sf_snapshot(conn: sqlite3.Connection, account_id: int, submission_id: str, views: int,
                       favorites_count: int, comments_count: int, polled_at: str | None = None) -> None:
    ts = polled_at or datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    conn.execute(
        "INSERT INTO sf_snapshots (account_id, submission_id, polled_at, views, favorites_count, comments_count) VALUES (?, ?, ?, ?, ?, ?)",
        (account_id, submission_id, ts, views, favorites_count, comments_count),
    )


def get_sf_snapshots(conn: sqlite3.Connection, submission_id: str,
                     start: str | None = None, end: str | None = None) -> list[dict]:
    sql = "SELECT * FROM sf_snapshots WHERE submission_id = ?"
    params: list[Any] = [submission_id]
    if start:
        sql += " AND polled_at >= ?"
        params.append(start)
    if end:
        sql += " AND polled_at <= ?"
        params.append(end)
    sql += " ORDER BY polled_at ASC"
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def get_sf_aggregate_snapshots(conn: sqlite3.Connection, start: str | None = None,
                               end: str | None = None, account_id: int | None = None) -> list[dict]:
    sql = ("SELECT polled_at, SUM(views) as views, SUM(favorites_count) as favorites_count, "
           "SUM(comments_count) as comments_count FROM sf_snapshots")
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


def get_sf_comparison_snapshots(conn: sqlite3.Connection, submission_ids: list[str],
                                start: str | None = None, end: str | None = None) -> dict[str, list[dict]]:
    """Multi-submission time-series. One IN-clause query instead of N SELECTs."""
    result: dict[str, list[dict]] = {sid: [] for sid in submission_ids}
    if not submission_ids:
        return result
    placeholders = ",".join("?" * len(submission_ids))
    sql = f"SELECT * FROM sf_snapshots WHERE submission_id IN ({placeholders})"
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


# -- SF Poll Log -------------------------------------------------------

def start_sf_poll_log(conn: sqlite3.Connection, account_id: int = 0) -> int:
    cur = conn.execute(
        "INSERT INTO sf_poll_log (started_at, status, account_id) VALUES (datetime('now'), 'running', ?)",
        (account_id,))
    conn.commit()
    return cur.lastrowid


def finish_sf_poll_log(conn: sqlite3.Connection, log_id: int, status: str,
                       submissions_found: int = 0, snapshots_inserted: int = 0,
                       new_watchers_found: int = 0,
                       error_message: str | None = None, duration_seconds: float = 0) -> None:
    conn.execute(
        """UPDATE sf_poll_log SET finished_at=datetime('now'), status=?, submissions_found=?,
           snapshots_inserted=?, new_watchers_found=?, error_message=?, duration_seconds=?
           WHERE id=?""",
        (status, submissions_found, snapshots_inserted, new_watchers_found, error_message, duration_seconds, log_id),
    )
    conn.commit()


def get_sf_last_poll(conn: sqlite3.Connection) -> dict | None:
    row = conn.execute("SELECT * FROM sf_poll_log ORDER BY started_at DESC LIMIT 1").fetchone()
    return dict(row) if row else None


def get_sf_poll_log(conn: sqlite3.Connection, limit: int = 50) -> list[dict]:
    rows = conn.execute("SELECT * FROM sf_poll_log ORDER BY started_at DESC LIMIT ?", (limit,)).fetchall()
    return [dict(r) for r in rows]


# -- SF Watchers/Followers ---------------------------------------------

def upsert_sf_watcher(conn: sqlite3.Connection, account_id: int, username: str) -> bool:
    """Insert a watcher for this account if not already known. Returns True if new."""
    existing = conn.execute(
        "SELECT id FROM sf_watchers WHERE account_id = ? AND username = ?",
        (account_id, username)).fetchone()
    if existing:
        return False
    conn.execute("INSERT INTO sf_watchers (account_id, username) VALUES (?, ?)", (account_id, username))
    return True


def remove_stale_sf_watchers(conn: sqlite3.Connection, account_id: int, current_usernames: list[str]) -> int:
    """Remove this account's followers no longer on the live list. Returns rows deleted."""
    if not current_usernames:
        return 0
    placeholders = ",".join("?" for _ in current_usernames)
    cur = conn.execute(
        f"DELETE FROM sf_watchers WHERE account_id = ? AND username NOT IN ({placeholders})",
        [account_id, *current_usernames],
    )
    return cur.rowcount


def get_sf_watchers(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute("SELECT * FROM sf_watchers ORDER BY first_seen_at DESC").fetchall()
    return [dict(r) for r in rows]


def get_sf_watchers_count(conn: sqlite3.Connection) -> int:
    """Total number of tracked SF followers."""
    row = conn.execute("SELECT COUNT(*) as c FROM sf_watchers").fetchone()
    return row["c"] if row else 0


def get_sf_recent_watchers(conn: sqlite3.Connection, limit: int = 20) -> list[dict]:
    """Most recent SF followers, newest first."""
    rows = conn.execute(
        "SELECT * FROM sf_watchers ORDER BY first_seen_at DESC LIMIT ?",
        (limit,),
    ).fetchall()
    return [dict(r) for r in rows]


# -- SF Summary --------------------------------------------------------

def get_sf_summary(conn: sqlite3.Connection, account_id: int | None = None) -> dict:
    """Dashboard summary for SoFurry — mirrors ws_queries.get_ws_summary.

    With *account_id* set, every total/top-list is scoped to that account; the
    "All accounts" default (account_id=None) keeps the aggregate behaviour.
    """
    where, wp = account_clause(account_id)
    w = f" WHERE {where}" if where else ""
    totals = conn.execute(
        "SELECT COUNT(*) as total_submissions, COALESCE(SUM(views),0) as total_views, "
        "COALESCE(SUM(favorites_count),0) as total_favorites, COALESCE(SUM(comments_count),0) as total_comments "
        "FROM sf_submissions" + w,
        wp,
    ).fetchone()
    totals = dict(totals)

    top_viewed = conn.execute(
        "SELECT submission_id, title, views, thumbnail_url as thumb_url FROM sf_submissions" + w + " ORDER BY views DESC LIMIT 5",
        wp,
    ).fetchall()

    top_faved = conn.execute(
        "SELECT submission_id, title, favorites_count, thumbnail_url as thumb_url FROM sf_submissions" + w + " ORDER BY favorites_count DESC LIMIT 5",
        wp,
    ).fetchall()

    # Fastest-growing: only the outer `s` (sf_submissions) needs account scoping —
    # submission_ids are account-unique, so the snapshot subquery is implicitly correct.
    sw, sp = account_clause(account_id, "s")
    fastest_growing = conn.execute(
        """SELECT s.submission_id, s.title, s.thumbnail_url as thumb_url,
                  COALESCE(s.views - oldest.views, 0) as views_gained,
                  COALESCE(s.favorites_count - oldest.favorites_count, 0) as faves_gained
           FROM sf_submissions s
           LEFT JOIN (
               SELECT s1.submission_id, s1.views, s1.favorites_count
               FROM sf_snapshots s1
               INNER JOIN (
                   SELECT submission_id, MAX(polled_at) as max_polled
                   FROM sf_snapshots
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
        "top_viewed": [dict(r) for r in top_viewed],
        "top_faved": [dict(r) for r in top_faved],
        "fastest_growing": [dict(r) for r in fastest_growing],
    }


# -- SF Growth Rates ---------------------------------------------------

def _calc_growth_rate(current: int, past: int | None, hours: int) -> float | None:
    if past is None:
        return None
    delta = current - past
    days = hours / 24.0
    return round(delta / days, 2) if days > 0 else None


def get_sf_growth_rates(conn: sqlite3.Connection) -> dict:
    """Aggregate SF growth rates for 24h, 7d, 30d."""
    totals = conn.execute(
        "SELECT COALESCE(SUM(views),0) as views, COALESCE(SUM(favorites_count),0) as faves, "
        "COALESCE(SUM(comments_count),0) as comments FROM sf_submissions"
    ).fetchone()
    current_views = totals["views"]
    current_faves = totals["faves"]
    current_comments = totals["comments"]

    rates = {}
    for label, hours in [("24h", 24), ("7d", 168), ("30d", 720)]:
        row = conn.execute(
            """SELECT SUM(views) as views, SUM(favorites_count) as faves, SUM(comments_count) as comments
               FROM sf_snapshots WHERE polled_at = (
                   SELECT polled_at FROM sf_snapshots
                   WHERE polled_at <= datetime('now', ? || ' hours')
                   ORDER BY polled_at DESC LIMIT 1
               )""",
            (str(-hours),),
        ).fetchone()
        past_views = row["views"] if row and row["views"] is not None else None
        past_faves = row["faves"] if row and row["faves"] is not None else None
        past_comments = row["comments"] if row and row["comments"] is not None else None
        rates[label] = {
            "views_per_day": _calc_growth_rate(current_views, past_views, hours),
            "faves_per_day": _calc_growth_rate(current_faves, past_faves, hours),
            "comments_per_day": _calc_growth_rate(current_comments, past_comments, hours),
        }
    return rates


def get_sf_submission_growth_rates(conn: sqlite3.Connection, submission_id: str) -> dict:
    """Per-submission SF growth rates for 24h, 7d, 30d."""
    sub = conn.execute(
        "SELECT views, favorites_count, comments_count FROM sf_submissions WHERE submission_id = ?",
        (submission_id,),
    ).fetchone()
    if not sub:
        return {}

    rates = {}
    for label, hours in [("24h", 24), ("7d", 168), ("30d", 720)]:
        row = conn.execute(
            """SELECT views, favorites_count as faves, comments_count as comments
               FROM sf_snapshots WHERE submission_id = ? AND polled_at <= datetime('now', ? || ' hours')
               ORDER BY polled_at DESC LIMIT 1""",
            (submission_id, str(-hours)),
        ).fetchone()
        past_views = row["views"] if row else None
        past_faves = row["faves"] if row else None
        past_comments = row["comments"] if row else None
        rates[label] = {
            "views_per_day": _calc_growth_rate(sub["views"], past_views, hours),
            "faves_per_day": _calc_growth_rate(sub["favorites_count"], past_faves, hours),
            "comments_per_day": _calc_growth_rate(sub["comments_count"], past_comments, hours),
        }
    return rates


def get_sf_submission_deltas(conn: sqlite3.Connection) -> dict[str, dict]:
    """24h deltas for each SF submission."""
    rows = conn.execute(
        """SELECT s.submission_id,
                  COALESCE(s.views - old.views, 0) as views_delta,
                  COALESCE(s.favorites_count - old.favorites_count, 0) as faves_delta,
                  COALESCE(s.comments_count - old.comments_count, 0) as comments_delta
           FROM sf_submissions s
           LEFT JOIN (
               SELECT s1.submission_id, s1.views, s1.favorites_count, s1.comments_count
               FROM sf_snapshots s1
               INNER JOIN (
                   SELECT submission_id, MAX(polled_at) as max_polled
                   FROM sf_snapshots
                   WHERE polled_at <= datetime('now', '-24 hours')
                   GROUP BY submission_id
               ) s2 ON s1.submission_id = s2.submission_id AND s1.polled_at = s2.max_polled
           ) old ON s.submission_id = old.submission_id"""
    ).fetchall()
    return {r["submission_id"]: dict(r) for r in rows}
