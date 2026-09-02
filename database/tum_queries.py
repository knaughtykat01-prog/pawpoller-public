"""All SQL CRUD functions for the Tumblr (TUM) analytics database.

Read-only polling via the Tumblr v2 API (API key + blog identifier).

Key differences from other platforms:
  - submission_id is TEXT (numeric id_string)
  - Single engagement metric: notes (note_count = likes + reblogs + replies)
  - No views, no individual comment tracking
  - content_type is Tumblr's post type (text/photo/quote/link/...)
"""

from __future__ import annotations
import json
from database.scope import account_clause  # optional `account_id = ?` WHERE-injection
import sqlite3
from datetime import datetime, timezone
from typing import Any


# -- TUM Submissions ---------------------------------------------------------

def upsert_tum_submission(conn: sqlite3.Connection, sub: dict, account_id: int) -> None:
    """Insert or update a Tumblr post's metadata and latest note count."""
    keywords_json = json.dumps(sub.get("keywords", []))
    conn.execute(
        """INSERT INTO tum_submissions
           (submission_id, account_id, title, full_text, username, posted_at, content_type,
            rating, description, keywords, link, thumbnail_url,
            notes, has_media, embed_type, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
           ON CONFLICT(submission_id) DO UPDATE SET
            title=excluded.title, full_text=excluded.full_text,
            username=excluded.username, content_type=excluded.content_type,
            rating=excluded.rating, description=excluded.description,
            keywords=excluded.keywords, link=excluded.link,
            thumbnail_url=excluded.thumbnail_url,
            notes=excluded.notes,
            has_media=excluded.has_media, embed_type=excluded.embed_type,
            updated_at=datetime('now')
        """,
        (
            sub["post_uri"], account_id, sub.get("title", ""), sub.get("full_text", ""),
            sub.get("username", ""), sub.get("posted_at"),
            sub.get("content_type", "text"), sub.get("rating", ""),
            sub.get("description", ""), keywords_json,
            sub.get("link", ""), sub.get("thumbnail_url", ""),
            sub.get("notes", 0),
            sub.get("has_media", 0), sub.get("embed_type", ""),
        ),
    )


def get_tum_submission(conn: sqlite3.Connection, submission_id: str) -> dict | None:
    row = conn.execute("SELECT * FROM tum_submissions WHERE submission_id = ?", (submission_id,)).fetchone()
    return dict(row) if row else None


def get_all_tum_submissions(conn: sqlite3.Connection, sort_by: str = "notes", order: str = "desc", account_id: int | None = None) -> list[dict]:
    allowed_sorts = {"notes", "title", "posted_at", "updated_at"}
    if sort_by not in allowed_sorts:
        sort_by = "notes"
    order_dir = "DESC" if order.lower() == "desc" else "ASC"
    where, params = account_clause(account_id)
    sql = "SELECT * FROM tum_submissions" + (f" WHERE {where}" if where else "")
    sql += f" ORDER BY {sort_by} {order_dir}"
    rows = conn.execute(sql, params).fetchall()
    return [dict(r) for r in rows]


# -- TUM Snapshots -----------------------------------------------------------

def insert_tum_snapshot(conn: sqlite3.Connection, account_id: int, submission_id: str,
                        notes: int, polled_at: str | None = None) -> None:
    ts = polled_at or datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    conn.execute(
        "INSERT INTO tum_snapshots (account_id, submission_id, polled_at, notes) VALUES (?, ?, ?, ?)",
        (account_id, submission_id, ts, notes),
    )


def get_tum_snapshots(conn: sqlite3.Connection, submission_id: str,
                      start: str | None = None, end: str | None = None) -> list[dict]:
    sql = "SELECT * FROM tum_snapshots WHERE submission_id = ?"
    params: list[Any] = [submission_id]
    if start:
        sql += " AND polled_at >= ?"
        params.append(start)
    if end:
        sql += " AND polled_at <= ?"
        params.append(end)
    sql += " ORDER BY polled_at ASC"
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def get_tum_aggregate_snapshots(conn: sqlite3.Connection, start: str | None = None,
                                end: str | None = None, account_id: int | None = None) -> list[dict]:
    sql = "SELECT polled_at, SUM(notes) as notes FROM tum_snapshots"
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


def get_tum_comparison_snapshots(conn: sqlite3.Connection, submission_ids: list[str],
                                 start: str | None = None, end: str | None = None) -> dict[str, list[dict]]:
    result: dict[str, list[dict]] = {sid: [] for sid in submission_ids}
    if not submission_ids:
        return result
    placeholders = ",".join("?" * len(submission_ids))
    sql = f"SELECT * FROM tum_snapshots WHERE submission_id IN ({placeholders})"
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


# -- TUM Poll Log ------------------------------------------------------------

def start_tum_poll_log(conn: sqlite3.Connection, account_id: int = 0) -> int:
    cur = conn.execute(
        "INSERT INTO tum_poll_log (started_at, status, account_id) VALUES (datetime('now'), 'running', ?)",
        (account_id,))
    conn.commit()
    return cur.lastrowid


def finish_tum_poll_log(conn: sqlite3.Connection, log_id: int, status: str,
                        submissions_found: int = 0, snapshots_inserted: int = 0,
                        error_message: str | None = None, duration_seconds: float = 0) -> None:
    conn.execute(
        """UPDATE tum_poll_log SET finished_at=datetime('now'), status=?, submissions_found=?,
           snapshots_inserted=?, error_message=?, duration_seconds=?
           WHERE id=?""",
        (status, submissions_found, snapshots_inserted,
         error_message, duration_seconds, log_id),
    )
    conn.commit()


def get_tum_last_poll(conn: sqlite3.Connection) -> dict | None:
    row = conn.execute("SELECT * FROM tum_poll_log ORDER BY started_at DESC LIMIT 1").fetchone()
    return dict(row) if row else None


def get_tum_poll_log(conn: sqlite3.Connection, limit: int = 50) -> list[dict]:
    rows = conn.execute("SELECT * FROM tum_poll_log ORDER BY started_at DESC LIMIT ?", (limit,)).fetchall()
    return [dict(r) for r in rows]


# -- TUM Summary -------------------------------------------------------------

def get_tum_summary(conn: sqlite3.Connection, account_id: int | None = None) -> dict:
    """Dashboard summary for Tumblr. With *account_id* set, totals + top-lists
    scope to that account; account_id=None aggregates across accounts."""
    where, wp = account_clause(account_id)
    w = f" WHERE {where}" if where else ""
    totals = conn.execute(
        "SELECT COUNT(*) as total_submissions, COALESCE(SUM(notes),0) as total_notes "
        "FROM tum_submissions" + w,
        wp,
    ).fetchone()
    totals = dict(totals)

    top_noted = conn.execute(
        "SELECT submission_id, title, notes FROM tum_submissions" + w + " ORDER BY notes DESC LIMIT 5",
        wp,
    ).fetchall()

    sw, sp = account_clause(account_id, "s")
    fastest_growing = conn.execute(
        """SELECT s.submission_id, s.title,
                  COALESCE(s.notes - oldest.notes, 0) as notes_gained
           FROM tum_submissions s
           LEFT JOIN (
               SELECT s1.submission_id, s1.notes
               FROM tum_snapshots s1
               INNER JOIN (
                   SELECT submission_id, MAX(polled_at) as max_polled
                   FROM tum_snapshots
                   WHERE polled_at <= datetime('now', '-24 hours')
                   GROUP BY submission_id
               ) s2 ON s1.submission_id = s2.submission_id AND s1.polled_at = s2.max_polled
           ) oldest ON s.submission_id = oldest.submission_id
           WHERE """ + (sw + " AND " if sw else "") + """COALESCE(s.notes - oldest.notes, 0) > 0
           ORDER BY notes_gained DESC LIMIT 5""",
        sp,
    ).fetchall()

    return {
        "total_submissions": totals["total_submissions"],
        "total_notes": totals["total_notes"],
        # cross-platform aggregation reads total_favorites as the engagement bucket
        "total_favorites": totals["total_notes"],
        "top_noted": [dict(r) for r in top_noted],
        "fastest_growing": [dict(r) for r in fastest_growing],
    }


# -- TUM Growth Rates --------------------------------------------------------

def _calc_growth_rate(current: int, past: int | None, hours: int) -> float | None:
    if past is None:
        return None
    delta = current - past
    days = hours / 24.0
    return round(delta / days, 2) if days > 0 else None


def get_tum_growth_rates(conn: sqlite3.Connection) -> dict:
    totals = conn.execute(
        "SELECT COALESCE(SUM(notes),0) as notes FROM tum_submissions"
    ).fetchone()
    current_notes = totals["notes"]

    rates = {}
    for label, hours in [("24h", 24), ("7d", 168), ("30d", 720)]:
        row = conn.execute(
            """SELECT SUM(notes) as notes
               FROM tum_snapshots WHERE polled_at = (
                   SELECT polled_at FROM tum_snapshots
                   WHERE polled_at <= datetime('now', ? || ' hours')
                   ORDER BY polled_at DESC LIMIT 1
               )""",
            (str(-hours),),
        ).fetchone()
        past_notes = row["notes"] if row and row["notes"] is not None else None
        rates[label] = {
            "notes_per_day": _calc_growth_rate(current_notes, past_notes, hours),
        }
    return rates


def get_tum_submission_growth_rates(conn: sqlite3.Connection, submission_id: str) -> dict:
    sub = conn.execute(
        "SELECT notes FROM tum_submissions WHERE submission_id = ?",
        (submission_id,),
    ).fetchone()
    if not sub:
        return {}

    rates = {}
    for label, hours in [("24h", 24), ("7d", 168), ("30d", 720)]:
        row = conn.execute(
            """SELECT notes
               FROM tum_snapshots WHERE submission_id = ? AND polled_at <= datetime('now', ? || ' hours')
               ORDER BY polled_at DESC LIMIT 1""",
            (submission_id, str(-hours)),
        ).fetchone()
        past_notes = row["notes"] if row else None
        rates[label] = {
            "notes_per_day": _calc_growth_rate(sub["notes"], past_notes, hours),
        }
    return rates


def get_tum_submission_deltas(conn: sqlite3.Connection) -> dict[str, dict]:
    rows = conn.execute(
        """SELECT s.submission_id,
                  COALESCE(s.notes - old.notes, 0) as notes_delta
           FROM tum_submissions s
           LEFT JOIN (
               SELECT s1.submission_id, s1.notes
               FROM tum_snapshots s1
               INNER JOIN (
                   SELECT submission_id, MAX(polled_at) as max_polled
                   FROM tum_snapshots
                   WHERE polled_at <= datetime('now', '-24 hours')
                   GROUP BY submission_id
               ) s2 ON s1.submission_id = s2.submission_id AND s1.polled_at = s2.max_polled
           ) old ON s.submission_id = old.submission_id"""
    ).fetchall()
    return {r["submission_id"]: dict(r) for r in rows}
