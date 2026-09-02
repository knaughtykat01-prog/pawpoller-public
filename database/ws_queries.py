"""All SQL CRUD functions for the Weasyl (WS) analytics database.

This module mirrors the structure of queries.py (Inkbunny) but operates on
ws_-prefixed tables. Key differences from both IB and FA:
  - NO faving_users tracking: Like FA, Weasyl does not expose individual
    fave user data. Only aggregate favorites_count is available.
  - NO individual comment tracking: Unlike both IB and FA, Weasyl does not
    provide comment data beyond the aggregate comments_count. There are no
    ws_comments or comment-related functions in this module at all.
  - NO stat offsets: Like FA, WS summary totals are used as-is without
    VIEWS_OFFSET / FAVORITES_OFFSET / COMMENTS_OFFSET adjustments.
  - Simpler poll log: Only tracks submissions_found and snapshots_inserted --
    no new_faves_found or new_comments_found since neither is tracked.
  - Different metadata columns: WS submissions have subtype and media_url
    instead of IB's type_name/page_count or FA's category/theme/species/gender.
"""

from __future__ import annotations
import json
from database.scope import account_clause  # optional `account_id = ?` WHERE-injection
import sqlite3
from datetime import datetime, timezone
from typing import Any


# ── WS Submissions ────────────────────────────────────────────

def upsert_ws_submission(conn: sqlite3.Connection, sub: dict, account_id: int) -> None:
    """Insert or update a Weasyl submission's metadata and latest stats.

    Same upsert pattern as IB and FA (INSERT ... ON CONFLICT ... DO UPDATE).
    Keywords are JSON-serialized to a TEXT column for the same reasons as
    the other platforms.

    WS-specific columns: subtype (visual/literary/multimedia), media_url
    (direct link to the media file), and link (Weasyl submission page URL).
    """
    # Serialize keywords list to JSON string, same pattern as IB and FA.
    keywords_json = json.dumps(sub.get("keywords", []))
    # account_id set on INSERT only; the ON CONFLICT UPDATE leaves it alone.
    conn.execute(
        """INSERT INTO ws_submissions
           (submission_id, account_id, title, username, posted_at, subtype,
            rating, thumbnail_url, media_url,
            description, keywords, link,
            views, favorites_count, comments_count, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
           ON CONFLICT(submission_id) DO UPDATE SET
            title=excluded.title, username=excluded.username,
            subtype=excluded.subtype,
            rating=excluded.rating, thumbnail_url=excluded.thumbnail_url,
            media_url=excluded.media_url, description=excluded.description,
            keywords=excluded.keywords, link=excluded.link,
            views=excluded.views, favorites_count=excluded.favorites_count,
            comments_count=excluded.comments_count, updated_at=datetime('now')
        """,
        (
            sub["submission_id"], account_id, sub.get("title", ""), sub.get("username", ""),
            sub.get("posted_at"), sub.get("subtype", ""),
            sub.get("rating", ""), sub.get("thumbnail_url", ""),
            sub.get("media_url", ""),
            sub.get("description", ""), keywords_json, sub.get("link", ""),
            sub.get("views", 0), sub.get("favorites_count", 0),
            sub.get("comments_count", 0),
        ),
    )


def get_ws_submission(conn: sqlite3.Connection, submission_id: int) -> dict | None:
    row = conn.execute("SELECT * FROM ws_submissions WHERE submission_id = ?", (submission_id,)).fetchone()
    return dict(row) if row else None


def get_ws_previous_favorites_count(conn: sqlite3.Connection, submission_id: int) -> int | None:
    """Get the favorites_count from the most recent WS snapshot.

    Used only for detecting count changes. Unlike IB, Weasyl does not
    expose individual user-level fave data at all -- only the aggregate count.
    """
    row = conn.execute(
        "SELECT favorites_count FROM ws_snapshots WHERE submission_id = ? ORDER BY polled_at DESC LIMIT 1",
        (submission_id,),
    ).fetchone()
    return row["favorites_count"] if row else None


def get_all_ws_submissions(conn: sqlite3.Connection, sort_by: str = "views", order: str = "desc", account_id: int | None = None) -> list[dict]:
    # Whitelist-based sort column validation, same pattern as IB and FA.
    allowed_sorts = {"views", "favorites_count", "comments_count", "title", "posted_at", "updated_at"}
    if sort_by not in allowed_sorts:
        sort_by = "views"
    order_dir = "DESC" if order.lower() == "desc" else "ASC"
    where, params = account_clause(account_id)
    sql = "SELECT * FROM ws_submissions" + (f" WHERE {where}" if where else "")
    sql += f" ORDER BY {sort_by} {order_dir}"
    rows = conn.execute(sql, params).fetchall()
    return [dict(r) for r in rows]


# ── WS Snapshots ──────────────────────────────────────────────
# Snapshot time-series for WS submissions. Same append-only pattern as IB
# and FA -- one row per submission per poll cycle.

def insert_ws_snapshot(conn: sqlite3.Connection, account_id: int, submission_id: int, views: int, favorites_count: int, comments_count: int, polled_at: str | None = None) -> None:
    # Append-only: each poll cycle adds a new row, never updates existing ones.
    ts = polled_at or datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    conn.execute(
        "INSERT INTO ws_snapshots (account_id, submission_id, polled_at, views, favorites_count, comments_count) VALUES (?, ?, ?, ?, ?, ?)",
        (account_id, submission_id, ts, views, favorites_count, comments_count),
    )


def get_ws_snapshots(conn: sqlite3.Connection, submission_id: int, start: str | None = None, end: str | None = None) -> list[dict]:
    """Per-submission time-series with optional date range filtering.
    Mirrors queries.get_snapshots for the ws_snapshots table."""
    sql = "SELECT * FROM ws_snapshots WHERE submission_id = ?"
    params: list[Any] = [submission_id]
    if start:
        sql += " AND polled_at >= ?"
        params.append(start)
    if end:
        sql += " AND polled_at <= ?"
        params.append(end)
    sql += " ORDER BY polled_at ASC"
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def get_ws_aggregate_snapshots(conn: sqlite3.Connection, start: str | None = None, end: str | None = None, account_id: int | None = None) -> list[dict]:
    """Aggregate time-series across all WS submissions per poll timestamp.
    Mirrors queries.get_aggregate_snapshots for the ws_snapshots table. With
    *account_id* set, the totals are scoped to that account."""
    sql = "SELECT polled_at, SUM(views) as views, SUM(favorites_count) as favorites_count, SUM(comments_count) as comments_count FROM ws_snapshots"
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


def get_ws_comparison_snapshots(conn: sqlite3.Connection, submission_ids: list[int], start: str | None = None, end: str | None = None) -> dict[int, list[dict]]:
    """Multi-submission time-series for comparison charts. One IN-clause query."""
    result: dict[int, list[dict]] = {sid: [] for sid in submission_ids}
    if not submission_ids:
        return result
    placeholders = ",".join("?" * len(submission_ids))
    sql = f"SELECT * FROM ws_snapshots WHERE submission_id IN ({placeholders})"
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


# ── WS Poll Log ───────────────────────────────────────────────
# Same poll audit logging pattern as IB and FA. Weasyl's poll log is the
# simplest of the three: it only tracks submissions_found and
# snapshots_inserted. There are no new_faves_found or new_comments_found
# fields because Weasyl does not expose individual fave users or comments.

def start_ws_poll_log(conn: sqlite3.Connection, account_id: int = 0) -> int:
    cur = conn.execute(
        "INSERT INTO ws_poll_log (started_at, status, account_id) VALUES (datetime('now'), 'running', ?)",
        (account_id,))
    conn.commit()
    return cur.lastrowid


def finish_ws_poll_log(conn: sqlite3.Connection, log_id: int, status: str, submissions_found: int = 0,
                       snapshots_inserted: int = 0,
                       error_message: str | None = None, duration_seconds: float = 0) -> None:
    # Simplest poll log finish: no new_faves_found (no fave user tracking)
    # and no new_comments_found (no comment tracking at all for WS).
    conn.execute(
        """UPDATE ws_poll_log SET finished_at=datetime('now'), status=?, submissions_found=?,
           snapshots_inserted=?, error_message=?, duration_seconds=?
           WHERE id=?""",
        (status, submissions_found, snapshots_inserted, error_message, duration_seconds, log_id),
    )
    conn.commit()


def get_ws_last_poll(conn: sqlite3.Connection) -> dict | None:
    row = conn.execute("SELECT * FROM ws_poll_log ORDER BY started_at DESC LIMIT 1").fetchone()
    return dict(row) if row else None


def get_ws_poll_log(conn: sqlite3.Connection, limit: int = 50) -> list[dict]:
    rows = conn.execute("SELECT * FROM ws_poll_log ORDER BY started_at DESC LIMIT ?", (limit,)).fetchall()
    return [dict(r) for r in rows]


# ── WS Summary Stats ──────────────────────────────────────────

def get_ws_summary(conn: sqlite3.Connection, account_id: int | None = None) -> dict:
    """Main dashboard data source for WS -- mirrors queries.get_summary.

    The simplest summary of the three platforms:
    - NO stat offsets (WS has no config offset constants)
    - NO recent_faves (WS lacks individual fave user tracking)
    - NO recent_comments (WS lacks individual comment tracking entirely)
    - Only returns totals, leaderboards, and fastest-growing submissions

    With *account_id* set, every total/top-list is scoped to that account; the
    "All accounts" default (account_id=None) keeps the aggregate behaviour.
    """
    where, wp = account_clause(account_id)
    w = f" WHERE {where}" if where else ""
    totals = conn.execute(
        "SELECT COUNT(*) as total_submissions, COALESCE(SUM(views),0) as total_views, "
        "COALESCE(SUM(favorites_count),0) as total_favorites, COALESCE(SUM(comments_count),0) as total_comments "
        "FROM ws_submissions" + w,
        wp,
    ).fetchone()
    # No offsets applied -- WS totals are used as-is.
    totals = dict(totals)

    top_viewed = conn.execute(
        "SELECT submission_id, title, views, thumbnail_url as thumb_url FROM ws_submissions" + w + " ORDER BY views DESC LIMIT 5",
        wp,
    ).fetchall()

    top_faved = conn.execute(
        "SELECT submission_id, title, favorites_count, thumbnail_url as thumb_url FROM ws_submissions" + w + " ORDER BY favorites_count DESC LIMIT 5",
        wp,
    ).fetchall()

    # Fastest-growing: same LEFT JOIN subquery pattern as IB and FA. Only the
    # outer `s` needs account scoping — submission_ids are unique to their
    # account, so the snapshot join is implicitly account-correct.
    sw, sp = account_clause(account_id, "s")
    fastest_growing = conn.execute(
        """SELECT s.submission_id, s.title, s.thumbnail_url as thumb_url,
                  COALESCE(s.views - oldest.views, 0) as views_gained,
                  COALESCE(s.favorites_count - oldest.favorites_count, 0) as faves_gained
           FROM ws_submissions s
           LEFT JOIN (
               SELECT s1.submission_id, s1.views, s1.favorites_count
               FROM ws_snapshots s1
               INNER JOIN (
                   SELECT submission_id, MAX(polled_at) as max_polled
                   FROM ws_snapshots
                   WHERE polled_at <= datetime('now', '-24 hours')
                   GROUP BY submission_id
               ) s2 ON s1.submission_id = s2.submission_id AND s1.polled_at = s2.max_polled
           ) oldest ON s.submission_id = oldest.submission_id
           WHERE """ + (sw + " AND " if sw else "") + """COALESCE(s.views - oldest.views, 0) > 0
           ORDER BY views_gained DESC LIMIT 5""",
        sp,
    ).fetchall()

    # No recent_faves or recent_comments -- WS does not track either.
    return {
        "total_submissions": totals["total_submissions"],
        "total_views": totals["total_views"],
        "total_favorites": totals["total_favorites"],
        "total_comments": totals["total_comments"],
        "top_viewed": [dict(r) for r in top_viewed],
        "top_faved": [dict(r) for r in top_faved],
        "fastest_growing": [dict(r) for r in fastest_growing],
    }


def _calc_growth_rate(current: int, past: int | None, hours: int) -> float | None:
    """Daily growth rate formula: (current - past) / (hours / 24).
    Same helper as in queries.py and fa_queries.py -- duplicated here to keep
    each module self-contained without cross-module imports."""
    if past is None:
        return None
    delta = current - past
    days = hours / 24.0
    return round(delta / days, 2) if days > 0 else None


def get_ws_growth_rates(conn: sqlite3.Connection) -> dict:
    """Aggregate WS growth rates for 24h, 7d, 30d.

    Same approach as queries.get_growth_rates and fa_queries.get_fa_growth_rates
    but without stat offsets -- WS does not track deleted/private submission
    stats separately.
    """
    totals = conn.execute(
        "SELECT COALESCE(SUM(views),0) as views, COALESCE(SUM(favorites_count),0) as faves, "
        "COALESCE(SUM(comments_count),0) as comments FROM ws_submissions"
    ).fetchone()
    # No offsets applied -- WS totals are used as-is.
    current_views = totals["views"]
    current_faves = totals["faves"]
    current_comments = totals["comments"]

    rates = {}
    for label, hours in [("24h", 24), ("7d", 168), ("30d", 720)]:
        # Find the nearest past snapshot timestamp and sum across all
        # WS submissions at that timestamp. Same subquery pattern as IB and FA.
        row = conn.execute(
            """SELECT SUM(views) as views, SUM(favorites_count) as faves, SUM(comments_count) as comments
               FROM ws_snapshots WHERE polled_at = (
                   SELECT polled_at FROM ws_snapshots
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


def get_ws_submission_growth_rates(conn: sqlite3.Connection, submission_id: int) -> dict:
    """Per-submission WS growth rates for 24h, 7d, 30d.
    Same approach as queries.get_submission_growth_rates on ws_snapshots."""
    sub = conn.execute(
        "SELECT views, favorites_count, comments_count FROM ws_submissions WHERE submission_id = ?",
        (submission_id,),
    ).fetchone()
    if not sub:
        return {}

    rates = {}
    for label, hours in [("24h", 24), ("7d", 168), ("30d", 720)]:
        row = conn.execute(
            """SELECT views, favorites_count as faves, comments_count as comments
               FROM ws_snapshots WHERE submission_id = ? AND polled_at <= datetime('now', ? || ' hours')
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


def get_ws_submission_deltas(conn: sqlite3.Connection) -> dict[int, dict]:
    """24h deltas for each WS submission.
    Same LEFT JOIN subquery pattern as queries.get_submission_deltas,
    operating on ws_submissions and ws_snapshots tables."""
    rows = conn.execute(
        """SELECT s.submission_id,
                  COALESCE(s.views - old.views, 0) as views_delta,
                  COALESCE(s.favorites_count - old.favorites_count, 0) as faves_delta,
                  COALESCE(s.comments_count - old.comments_count, 0) as comments_delta
           FROM ws_submissions s
           LEFT JOIN (
               SELECT s1.submission_id, s1.views, s1.favorites_count, s1.comments_count
               FROM ws_snapshots s1
               INNER JOIN (
                   SELECT submission_id, MAX(polled_at) as max_polled
                   FROM ws_snapshots
                   WHERE polled_at <= datetime('now', '-24 hours')
                   GROUP BY submission_id
               ) s2 ON s1.submission_id = s2.submission_id AND s1.polled_at = s2.max_polled
           ) old ON s.submission_id = old.submission_id"""
    ).fetchall()
    return {r["submission_id"]: dict(r) for r in rows}
