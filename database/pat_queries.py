"""SQL for the Patreon (`pat`) tables — spec 084. The Toyhouse query module's shape, trimmed.

Patreon shows no views, so likes (`favorites_count`) lead. `submission_id` is Patreon's post id as TEXT.
Supporter numbers are in ``database/supporter_queries.py``.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from typing import Any

from database.scope import account_clause


def upsert_pat_submission(conn: sqlite3.Connection, sub: dict, account_id: int) -> None:
    conn.execute(
        """INSERT INTO pat_submissions
           (submission_id, account_id, title, username, link, posted_at, audience,
            views, favorites_count, comments_count, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?, ?, datetime('now'))
           ON CONFLICT(submission_id) DO UPDATE SET
            title=excluded.title, username=excluded.username, link=excluded.link,
            posted_at=COALESCE(NULLIF(excluded.posted_at, ''), pat_submissions.posted_at),
            favorites_count=excluded.favorites_count, comments_count=excluded.comments_count,
            updated_at=datetime('now')
        """,
        (sub["submission_id"], account_id, sub.get("title", ""), sub.get("username", ""), sub.get("link", ""),
         sub.get("posted_at") or None, sub.get("audience", ""), int(sub.get("favorites_count") or 0),
         int(sub.get("comments_count") or 0)),
    )


def get_pat_submission(conn: sqlite3.Connection, submission_id: str) -> dict | None:
    row = conn.execute("SELECT * FROM pat_submissions WHERE submission_id = ?", (submission_id,)).fetchone()
    return dict(row) if row else None


def get_all_pat_submissions(conn: sqlite3.Connection, sort_by: str = "favorites_count",
                            order: str = "desc", account_id: int | None = None) -> list[dict]:
    if sort_by not in {"favorites_count", "comments_count", "title", "posted_at", "updated_at"}:
        sort_by = "favorites_count"
    order_dir = "DESC" if order.lower() == "desc" else "ASC"
    where, params = account_clause(account_id)
    sql = "SELECT * FROM pat_submissions" + (f" WHERE {where}" if where else "")
    return [dict(r) for r in conn.execute(sql + f" ORDER BY {sort_by} {order_dir}", params).fetchall()]


def insert_pat_snapshot(conn: sqlite3.Connection, account_id: int, submission_id: str,
                        favorites_count: int, comments_count: int, polled_at: str | None = None) -> None:
    ts = polled_at or datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    conn.execute(
        "INSERT INTO pat_snapshots (account_id, submission_id, polled_at, views, favorites_count, comments_count) "
        "VALUES (?, ?, ?, 0, ?, ?)", (account_id, submission_id, ts, int(favorites_count or 0), int(comments_count or 0)))


def get_pat_snapshots(conn: sqlite3.Connection, submission_id: str,
                      start: str | None = None, end: str | None = None) -> list[dict]:
    sql, params = "SELECT * FROM pat_snapshots WHERE submission_id = ?", [submission_id]
    if start:
        sql += " AND polled_at >= ?"
        params.append(start)
    if end:
        sql += " AND polled_at <= ?"
        params.append(end)
    return [dict(r) for r in conn.execute(sql + " ORDER BY polled_at ASC", params).fetchall()]


def start_pat_poll_log(conn: sqlite3.Connection, account_id: int = 0) -> int:
    cur = conn.execute("INSERT INTO pat_poll_log (started_at, status, account_id) VALUES (datetime('now'), 'running', ?)",
                       (account_id,))
    conn.commit()
    if cur.lastrowid is None:          # never after an INSERT; keeps the id an int
        raise sqlite3.DatabaseError("the Patreon poll log row wasn't written")
    return cur.lastrowid


def finish_pat_poll_log(conn: sqlite3.Connection, log_id: int, status: str, submissions_found: int = 0,
                        snapshots_inserted: int = 0, error_message: str | None = None,
                        duration_seconds: float = 0) -> None:
    conn.execute(
        "UPDATE pat_poll_log SET finished_at=datetime('now'), status=?, submissions_found=?, snapshots_inserted=?, "
        "error_message=?, duration_seconds=? WHERE id=?",
        (status, submissions_found, snapshots_inserted, error_message, duration_seconds, log_id))
    conn.commit()


def get_pat_last_poll(conn: sqlite3.Connection) -> dict | None:
    row = conn.execute("SELECT * FROM pat_poll_log ORDER BY started_at DESC LIMIT 1").fetchone()
    return dict(row) if row else None


def get_pat_poll_log(conn: sqlite3.Connection, limit: int = 50) -> list[dict]:
    return [dict(r) for r in conn.execute("SELECT * FROM pat_poll_log ORDER BY started_at DESC LIMIT ?",
                                          (limit,)).fetchall()]


def get_pat_summary(conn: sqlite3.Connection, account_id: int | None = None) -> dict:
    where, wp = account_clause(account_id)
    w = f" WHERE {where}" if where else ""
    totals = dict(conn.execute(
        "SELECT COUNT(*) AS total_submissions, COALESCE(SUM(favorites_count),0) AS total_favorites, "
        "COALESCE(SUM(comments_count),0) AS total_comments FROM pat_submissions" + w, wp).fetchone())
    top: list[Any] = conn.execute(
        "SELECT submission_id, title, favorites_count FROM pat_submissions" + w
        + " ORDER BY favorites_count DESC LIMIT 5", wp).fetchall()
    return {**totals, "total_views": 0, "top_faved": [dict(r) for r in top]}
