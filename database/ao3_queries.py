"""All SQL CRUD functions for the AO3 (Archive of Our Own) analytics database.

AO3 runs on OTW Archive software (same as SquidgeWorld). Compared to other
platforms in PawPoller, AO3 has an additional metric: bookmarks.

Key differences from other platforms:
  - submission_id is INTEGER (OTW Archive work IDs)
  - Has bookmarks_count in addition to views, kudos, comments
  - "Kudos" maps to favorites_count for schema consistency
  - Tracks individual kudos users (like IB's faving_users)
"""

from __future__ import annotations
import json
from database.scope import account_clause  # optional `account_id = ?` WHERE-injection
import sqlite3
from datetime import datetime, timezone
from typing import Any


# -- AO3 Submissions ---------------------------------------------------

def upsert_ao3_submission(conn: sqlite3.Connection, sub: dict, account_id: int) -> None:
    """Insert or update an AO3 work's metadata and latest stats."""
    keywords_json = json.dumps(sub.get("keywords", []))
    chapters = sub.get("chapters", "1/1")
    if isinstance(chapters, dict):
        chapters = f"{sub.get('chapters_current', 1)}/{sub.get('chapters_total', '1')}"
    elif not isinstance(chapters, str):
        cur = sub.get("chapters_current", 1)
        tot = sub.get("chapters_total", "1")
        chapters = f"{cur}/{tot}"

    # account_id set on INSERT only; the ON CONFLICT UPDATE leaves it alone.
    conn.execute(
        """INSERT INTO ao3_submissions
           (submission_id, account_id, title, username, posted_at, fandom, rating,
            description, keywords, link, word_count, chapters,
            views, favorites_count, comments_count, bookmarks_count, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
           ON CONFLICT(submission_id) DO UPDATE SET
            title=excluded.title, username=excluded.username,
            fandom=excluded.fandom, rating=excluded.rating,
            description=excluded.description, keywords=excluded.keywords,
            link=excluded.link, word_count=excluded.word_count,
            chapters=excluded.chapters, views=excluded.views,
            favorites_count=excluded.favorites_count,
            comments_count=excluded.comments_count,
            bookmarks_count=excluded.bookmarks_count,
            updated_at=datetime('now')
        """,
        (
            sub["work_id"], account_id, sub.get("title", ""), sub.get("username", ""),
            sub.get("posted_at"), sub.get("fandom", ""),
            sub.get("rating", ""), sub.get("description", ""),
            keywords_json, sub.get("link", ""),
            sub.get("word_count", 0), chapters,
            sub.get("views", 0), sub.get("favorites_count", 0),
            sub.get("comments_count", 0), sub.get("bookmarks_count", 0),
        ),
    )


def get_ao3_submission(conn: sqlite3.Connection, submission_id: int) -> dict | None:
    row = conn.execute("SELECT * FROM ao3_submissions WHERE submission_id = ?", (submission_id,)).fetchone()
    return dict(row) if row else None


def get_ao3_previous_comments_count(conn: sqlite3.Connection, submission_id: int) -> int | None:
    row = conn.execute(
        "SELECT comments_count FROM ao3_snapshots WHERE submission_id = ? ORDER BY polled_at DESC LIMIT 1",
        (submission_id,),
    ).fetchone()
    return row["comments_count"] if row else None


def get_all_ao3_submissions(conn: sqlite3.Connection, sort_by: str = "views", order: str = "desc", account_id: int | None = None) -> list[dict]:
    allowed_sorts = {"views", "favorites_count", "comments_count", "bookmarks_count",
                     "title", "posted_at", "updated_at", "word_count"}
    if sort_by not in allowed_sorts:
        sort_by = "views"
    order_dir = "DESC" if order.lower() == "desc" else "ASC"
    where, params = account_clause(account_id)
    sql = "SELECT * FROM ao3_submissions" + (f" WHERE {where}" if where else "")
    sql += f" ORDER BY {sort_by} {order_dir}"
    rows = conn.execute(sql, params).fetchall()
    return [dict(r) for r in rows]


# -- AO3 Snapshots -----------------------------------------------------

def insert_ao3_snapshot(conn: sqlite3.Connection, account_id: int, submission_id: int, views: int,
                        favorites_count: int, comments_count: int, bookmarks_count: int,
                        polled_at: str | None = None) -> None:
    ts = polled_at or datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    conn.execute(
        "INSERT INTO ao3_snapshots (account_id, submission_id, polled_at, views, favorites_count, comments_count, bookmarks_count) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (account_id, submission_id, ts, views, favorites_count, comments_count, bookmarks_count),
    )


def get_ao3_snapshots(conn: sqlite3.Connection, submission_id: int,
                      start: str | None = None, end: str | None = None) -> list[dict]:
    sql = "SELECT * FROM ao3_snapshots WHERE submission_id = ?"
    params: list[Any] = [submission_id]
    if start:
        sql += " AND polled_at >= ?"
        params.append(start)
    if end:
        sql += " AND polled_at <= ?"
        params.append(end)
    sql += " ORDER BY polled_at ASC"
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def get_ao3_aggregate_snapshots(conn: sqlite3.Connection, start: str | None = None,
                                end: str | None = None, account_id: int | None = None) -> list[dict]:
    sql = ("SELECT polled_at, SUM(views) as views, SUM(favorites_count) as favorites_count, "
           "SUM(comments_count) as comments_count, SUM(bookmarks_count) as bookmarks_count "
           "FROM ao3_snapshots")
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


def get_ao3_comparison_snapshots(conn: sqlite3.Connection, submission_ids: list[int],
                                 start: str | None = None, end: str | None = None) -> dict[str, list[dict]]:
    """Multi-submission time-series. One IN-clause query instead of N SELECTs."""
    result: dict[str, list[dict]] = {str(sid): [] for sid in submission_ids}
    if not submission_ids:
        return result
    placeholders = ",".join("?" * len(submission_ids))
    sql = f"SELECT * FROM ao3_snapshots WHERE submission_id IN ({placeholders})"
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


# -- AO3 Kudos Users ---------------------------------------------------

def upsert_ao3_kudos_user(conn: sqlite3.Connection, account_id: int, submission_id: int, username: str) -> bool:
    existing = conn.execute(
        "SELECT 1 FROM ao3_kudos_users WHERE submission_id = ? AND username = ?",
        (submission_id, username),
    ).fetchone()
    if existing:
        return False
    conn.execute(
        "INSERT INTO ao3_kudos_users (account_id, submission_id, username) VALUES (?, ?, ?)",
        (account_id, submission_id, username),
    )
    return True


def upsert_ao3_kudos_users_batch(conn: sqlite3.Connection, account_id: int, submission_id: int, usernames: list[str]) -> int:
    """Batch insert kudos users. Returns count of new kudos."""
    if not usernames:
        return 0
    before = conn.execute("SELECT COUNT(*) FROM ao3_kudos_users WHERE submission_id = ?", (submission_id,)).fetchone()[0]
    conn.executemany(
        "INSERT OR IGNORE INTO ao3_kudos_users (account_id, submission_id, username) VALUES (?, ?, ?)",
        [(account_id, submission_id, u) for u in usernames],
    )
    after = conn.execute("SELECT COUNT(*) FROM ao3_kudos_users WHERE submission_id = ?", (submission_id,)).fetchone()[0]
    return after - before


def get_ao3_kudos_users(conn: sqlite3.Connection, submission_id: int) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM ao3_kudos_users WHERE submission_id = ? ORDER BY first_seen_at DESC",
        (submission_id,),
    ).fetchall()
    return [dict(r) for r in rows]


# -- AO3 Poll Log ------------------------------------------------------

def start_ao3_poll_log(conn: sqlite3.Connection, account_id: int = 0) -> int:
    cur = conn.execute(
        "INSERT INTO ao3_poll_log (started_at, status, account_id) VALUES (datetime('now'), 'running', ?)",
        (account_id,))
    conn.commit()
    return cur.lastrowid


def finish_ao3_poll_log(conn: sqlite3.Connection, log_id: int, status: str,
                        submissions_found: int = 0, snapshots_inserted: int = 0,
                        new_kudos_found: int = 0,
                        error_message: str | None = None, duration_seconds: float = 0) -> None:
    conn.execute(
        """UPDATE ao3_poll_log SET finished_at=datetime('now'), status=?, submissions_found=?,
           snapshots_inserted=?, new_kudos_found=?, error_message=?, duration_seconds=?
           WHERE id=?""",
        (status, submissions_found, snapshots_inserted, new_kudos_found,
         error_message, duration_seconds, log_id),
    )
    conn.commit()


def get_ao3_last_poll(conn: sqlite3.Connection) -> dict | None:
    row = conn.execute("SELECT * FROM ao3_poll_log ORDER BY started_at DESC LIMIT 1").fetchone()
    return dict(row) if row else None


def get_ao3_poll_log(conn: sqlite3.Connection, limit: int = 50) -> list[dict]:
    rows = conn.execute("SELECT * FROM ao3_poll_log ORDER BY started_at DESC LIMIT ?", (limit,)).fetchall()
    return [dict(r) for r in rows]


# -- AO3 Summary -------------------------------------------------------

def get_ao3_summary(conn: sqlite3.Connection, account_id: int | None = None) -> dict:
    where, wp = account_clause(account_id)
    w = f" WHERE {where}" if where else ""
    totals = conn.execute(
        "SELECT COUNT(*) as total_submissions, COALESCE(SUM(views),0) as total_views, "
        "COALESCE(SUM(favorites_count),0) as total_favorites, "
        "COALESCE(SUM(comments_count),0) as total_comments, "
        "COALESCE(SUM(bookmarks_count),0) as total_bookmarks "
        "FROM ao3_submissions" + w,
        wp,
    ).fetchone()
    totals = dict(totals)

    top_viewed = conn.execute(
        "SELECT submission_id, title, views FROM ao3_submissions" + w + " ORDER BY views DESC LIMIT 5",
        wp,
    ).fetchall()

    top_faved = conn.execute(
        "SELECT submission_id, title, favorites_count FROM ao3_submissions" + w + " ORDER BY favorites_count DESC LIMIT 5",
        wp,
    ).fetchall()

    # Fastest-growing: only the outer `s` (ao3_submissions) needs account scoping —
    # work_ids are unique to their account, so the snapshot join is implicitly
    # account-correct.
    sw, sp = account_clause(account_id, "s")
    fastest_growing = conn.execute(
        """SELECT s.submission_id, s.title,
                  COALESCE(s.views - oldest.views, 0) as views_gained,
                  COALESCE(s.favorites_count - oldest.favorites_count, 0) as faves_gained
           FROM ao3_submissions s
           LEFT JOIN (
               SELECT s1.submission_id, s1.views, s1.favorites_count
               FROM ao3_snapshots s1
               INNER JOIN (
                   SELECT submission_id, MAX(polled_at) as max_polled
                   FROM ao3_snapshots
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
        "total_bookmarks": totals["total_bookmarks"],
        "top_viewed": [dict(r) for r in top_viewed],
        "top_faved": [dict(r) for r in top_faved],
        "fastest_growing": [dict(r) for r in fastest_growing],
    }


# -- AO3 Growth Rates --------------------------------------------------

def _calc_growth_rate(current: int, past: int | None, hours: int) -> float | None:
    if past is None:
        return None
    delta = current - past
    days = hours / 24.0
    return round(delta / days, 2) if days > 0 else None


def get_ao3_growth_rates(conn: sqlite3.Connection) -> dict:
    totals = conn.execute(
        "SELECT COALESCE(SUM(views),0) as views, COALESCE(SUM(favorites_count),0) as faves, "
        "COALESCE(SUM(comments_count),0) as comments, COALESCE(SUM(bookmarks_count),0) as bookmarks "
        "FROM ao3_submissions"
    ).fetchone()
    current_views = totals["views"]
    current_faves = totals["faves"]
    current_comments = totals["comments"]
    current_bookmarks = totals["bookmarks"]

    rates = {}
    for label, hours in [("24h", 24), ("7d", 168), ("30d", 720)]:
        row = conn.execute(
            """SELECT SUM(views) as views, SUM(favorites_count) as faves,
                      SUM(comments_count) as comments, SUM(bookmarks_count) as bookmarks
               FROM ao3_snapshots WHERE polled_at = (
                   SELECT polled_at FROM ao3_snapshots
                   WHERE polled_at <= datetime('now', ? || ' hours')
                   ORDER BY polled_at DESC LIMIT 1
               )""",
            (str(-hours),),
        ).fetchone()
        past_views = row["views"] if row and row["views"] is not None else None
        past_faves = row["faves"] if row and row["faves"] is not None else None
        past_comments = row["comments"] if row and row["comments"] is not None else None
        past_bookmarks = row["bookmarks"] if row and row["bookmarks"] is not None else None
        rates[label] = {
            "views_per_day": _calc_growth_rate(current_views, past_views, hours),
            "faves_per_day": _calc_growth_rate(current_faves, past_faves, hours),
            "comments_per_day": _calc_growth_rate(current_comments, past_comments, hours),
            "bookmarks_per_day": _calc_growth_rate(current_bookmarks, past_bookmarks, hours),
        }
    return rates


def get_ao3_submission_growth_rates(conn: sqlite3.Connection, submission_id: int) -> dict:
    sub = conn.execute(
        "SELECT views, favorites_count, comments_count, bookmarks_count FROM ao3_submissions WHERE submission_id = ?",
        (submission_id,),
    ).fetchone()
    if not sub:
        return {}

    rates = {}
    for label, hours in [("24h", 24), ("7d", 168), ("30d", 720)]:
        row = conn.execute(
            """SELECT views, favorites_count as faves, comments_count as comments, bookmarks_count as bookmarks
               FROM ao3_snapshots WHERE submission_id = ? AND polled_at <= datetime('now', ? || ' hours')
               ORDER BY polled_at DESC LIMIT 1""",
            (submission_id, str(-hours)),
        ).fetchone()
        past_views = row["views"] if row else None
        past_faves = row["faves"] if row else None
        past_comments = row["comments"] if row else None
        past_bookmarks = row["bookmarks"] if row else None
        rates[label] = {
            "views_per_day": _calc_growth_rate(sub["views"], past_views, hours),
            "faves_per_day": _calc_growth_rate(sub["favorites_count"], past_faves, hours),
            "comments_per_day": _calc_growth_rate(sub["comments_count"], past_comments, hours),
            "bookmarks_per_day": _calc_growth_rate(sub["bookmarks_count"], past_bookmarks, hours),
        }
    return rates


def get_ao3_submission_deltas(conn: sqlite3.Connection) -> dict[str, dict]:
    rows = conn.execute(
        """SELECT s.submission_id,
                  COALESCE(s.views - old.views, 0) as views_delta,
                  COALESCE(s.favorites_count - old.favorites_count, 0) as faves_delta,
                  COALESCE(s.comments_count - old.comments_count, 0) as comments_delta,
                  COALESCE(s.bookmarks_count - old.bookmarks_count, 0) as bookmarks_delta
           FROM ao3_submissions s
           LEFT JOIN (
               SELECT s1.submission_id, s1.views, s1.favorites_count, s1.comments_count, s1.bookmarks_count
               FROM ao3_snapshots s1
               INNER JOIN (
                   SELECT submission_id, MAX(polled_at) as max_polled
                   FROM ao3_snapshots
                   WHERE polled_at <= datetime('now', '-24 hours')
                   GROUP BY submission_id
               ) s2 ON s1.submission_id = s2.submission_id AND s1.polled_at = s2.max_polled
           ) old ON s.submission_id = old.submission_id"""
    ).fetchall()
    return {str(r["submission_id"]): dict(r) for r in rows}
