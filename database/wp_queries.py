"""All SQL CRUD functions for the Wattpad (WP) analytics database.

Wattpad provides a public JSON API at api.wattpad.com. No authentication
is required — only a target username is needed to discover and track stories.

Key differences from other platforms:
  - submission_id is INTEGER (Wattpad story IDs)
  - Uses reads instead of views, votes instead of favorites_count
  - num_lists (reading lists) is the unique Wattpad metric
  - Tracks word_count, num_parts, completed status, cover_url
  - No kudos/fave/comment individual user tracking — just counts
"""

from __future__ import annotations
import json
from database.scope import account_clause  # optional `account_id = ?` WHERE-injection
import sqlite3
from datetime import datetime, timezone
from typing import Any


# -- WP Submissions -------------------------------------------------------

def upsert_wp_submission(conn: sqlite3.Connection, sub: dict, account_id: int) -> None:
    """Insert or update a Wattpad story's metadata and latest stats."""
    keywords_json = json.dumps(sub.get("keywords", []))

    # account_id set on INSERT only; the ON CONFLICT UPDATE leaves it alone.
    conn.execute(
        """INSERT INTO wp_submissions
           (submission_id, account_id, title, username, posted_at, category, rating,
            description, keywords, link, cover_url, word_count, num_parts,
            completed, reads, votes, comments_count, num_lists, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
           ON CONFLICT(submission_id) DO UPDATE SET
            title=excluded.title, username=excluded.username,
            category=excluded.category, rating=excluded.rating,
            description=excluded.description, keywords=excluded.keywords,
            link=excluded.link, cover_url=excluded.cover_url,
            word_count=excluded.word_count, num_parts=excluded.num_parts,
            completed=excluded.completed, reads=excluded.reads,
            votes=excluded.votes, comments_count=excluded.comments_count,
            num_lists=excluded.num_lists, updated_at=datetime('now')
        """,
        (
            sub["story_id"], account_id, sub.get("title", ""), sub.get("username", ""),
            sub.get("posted_at"), sub.get("category", ""),
            sub.get("rating", ""), sub.get("description", ""),
            keywords_json, sub.get("link", ""), sub.get("cover_url", ""),
            sub.get("word_count", 0), sub.get("num_parts", 0),
            1 if sub.get("completed") else 0,
            sub.get("reads", 0), sub.get("votes", 0),
            sub.get("comments_count", 0), sub.get("num_lists", 0),
        ),
    )


def get_wp_submission(conn: sqlite3.Connection, submission_id: int) -> dict | None:
    row = conn.execute("SELECT * FROM wp_submissions WHERE submission_id = ?", (submission_id,)).fetchone()
    return dict(row) if row else None


def get_wp_previous_comments_count(conn: sqlite3.Connection, submission_id: int) -> int | None:
    row = conn.execute(
        "SELECT comments_count FROM wp_snapshots WHERE submission_id = ? ORDER BY polled_at DESC LIMIT 1",
        (submission_id,),
    ).fetchone()
    return row["comments_count"] if row else None


def get_all_wp_submissions(conn: sqlite3.Connection, sort_by: str = "reads", order: str = "desc", account_id: int | None = None) -> list[dict]:
    allowed_sorts = {"reads", "votes", "comments_count", "num_lists",
                     "title", "posted_at", "updated_at", "word_count"}
    if sort_by not in allowed_sorts:
        sort_by = "reads"
    order_dir = "DESC" if order.lower() == "desc" else "ASC"
    where, params = account_clause(account_id)
    sql = "SELECT * FROM wp_submissions" + (f" WHERE {where}" if where else "")
    sql += f" ORDER BY {sort_by} {order_dir}"
    rows = conn.execute(sql, params).fetchall()
    return [dict(r) for r in rows]


# -- WP Snapshots ---------------------------------------------------------

def insert_wp_snapshot(conn: sqlite3.Connection, account_id: int, submission_id: int, reads: int,
                       votes: int, comments_count: int, num_lists: int,
                       polled_at: str | None = None) -> None:
    ts = polled_at or datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    conn.execute(
        "INSERT INTO wp_snapshots (account_id, submission_id, polled_at, reads, votes, comments_count, num_lists) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (account_id, submission_id, ts, reads, votes, comments_count, num_lists),
    )


def get_wp_snapshots(conn: sqlite3.Connection, submission_id: int,
                     start: str | None = None, end: str | None = None) -> list[dict]:
    sql = "SELECT * FROM wp_snapshots WHERE submission_id = ?"
    params: list[Any] = [submission_id]
    if start:
        sql += " AND polled_at >= ?"
        params.append(start)
    if end:
        sql += " AND polled_at <= ?"
        params.append(end)
    sql += " ORDER BY polled_at ASC"
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def get_wp_aggregate_snapshots(conn: sqlite3.Connection, start: str | None = None,
                               end: str | None = None, account_id: int | None = None) -> list[dict]:
    sql = ("SELECT polled_at, SUM(reads) as reads, SUM(votes) as votes, "
           "SUM(comments_count) as comments_count, SUM(num_lists) as num_lists "
           "FROM wp_snapshots")
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


def get_wp_comparison_snapshots(conn: sqlite3.Connection, submission_ids: list[int],
                                start: str | None = None, end: str | None = None) -> dict[str, list[dict]]:
    """Multi-submission time-series. One IN-clause query instead of N SELECTs."""
    result: dict[str, list[dict]] = {str(sid): [] for sid in submission_ids}
    if not submission_ids:
        return result
    placeholders = ",".join("?" * len(submission_ids))
    sql = f"SELECT * FROM wp_snapshots WHERE submission_id IN ({placeholders})"
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


# -- WP Poll Log ----------------------------------------------------------

def start_wp_poll_log(conn: sqlite3.Connection, account_id: int = 0) -> int:
    cur = conn.execute(
        "INSERT INTO wp_poll_log (started_at, status, account_id) VALUES (datetime('now'), 'running', ?)",
        (account_id,))
    conn.commit()
    return cur.lastrowid


def finish_wp_poll_log(conn: sqlite3.Connection, log_id: int, status: str,
                       submissions_found: int = 0, snapshots_inserted: int = 0,
                       error_message: str | None = None, duration_seconds: float = 0) -> None:
    conn.execute(
        """UPDATE wp_poll_log SET finished_at=datetime('now'), status=?, submissions_found=?,
           snapshots_inserted=?, error_message=?, duration_seconds=?
           WHERE id=?""",
        (status, submissions_found, snapshots_inserted,
         error_message, duration_seconds, log_id),
    )
    conn.commit()


def get_wp_last_poll(conn: sqlite3.Connection) -> dict | None:
    row = conn.execute("SELECT * FROM wp_poll_log ORDER BY started_at DESC LIMIT 1").fetchone()
    return dict(row) if row else None


def get_wp_poll_log(conn: sqlite3.Connection, limit: int = 50) -> list[dict]:
    rows = conn.execute("SELECT * FROM wp_poll_log ORDER BY started_at DESC LIMIT ?", (limit,)).fetchall()
    return [dict(r) for r in rows]


# -- WP Summary -----------------------------------------------------------

def get_wp_summary(conn: sqlite3.Connection, account_id: int | None = None) -> dict:
    """Main dashboard data source for Wattpad.

    With *account_id* set, every total/top-list is scoped to that account; the
    "All accounts" default (account_id=None) keeps the aggregate behaviour.
    Wattpad has no per-user fave/comment tracking, so there are no recent-activity
    feeds to scope here (just counts).
    """
    where, wp = account_clause(account_id)
    w = f" WHERE {where}" if where else ""
    totals = conn.execute(
        "SELECT COUNT(*) as total_submissions, COALESCE(SUM(reads),0) as total_reads, "
        "COALESCE(SUM(votes),0) as total_votes, "
        "COALESCE(SUM(comments_count),0) as total_comments, "
        "COALESCE(SUM(num_lists),0) as total_lists "
        "FROM wp_submissions" + w,
        wp,
    ).fetchone()
    totals = dict(totals)

    top_read = conn.execute(
        "SELECT submission_id, title, reads FROM wp_submissions" + w + " ORDER BY reads DESC LIMIT 5",
        wp,
    ).fetchall()

    top_voted = conn.execute(
        "SELECT submission_id, title, votes FROM wp_submissions" + w + " ORDER BY votes DESC LIMIT 5",
        wp,
    ).fetchall()

    # Fastest-growing: only the outer `s` (wp_submissions) needs account scoping —
    # submission_ids are unique to their account, so the snapshot join is
    # implicitly account-correct.
    sw, sp = account_clause(account_id, "s")
    fastest_growing = conn.execute(
        """SELECT s.submission_id, s.title,
                  COALESCE(s.reads - oldest.reads, 0) as reads_gained,
                  COALESCE(s.votes - oldest.votes, 0) as votes_gained
           FROM wp_submissions s
           LEFT JOIN (
               SELECT s1.submission_id, s1.reads, s1.votes
               FROM wp_snapshots s1
               INNER JOIN (
                   SELECT submission_id, MAX(polled_at) as max_polled
                   FROM wp_snapshots
                   WHERE polled_at <= datetime('now', '-24 hours')
                   GROUP BY submission_id
               ) s2 ON s1.submission_id = s2.submission_id AND s1.polled_at = s2.max_polled
           ) oldest ON s.submission_id = oldest.submission_id
           WHERE """ + (sw + " AND " if sw else "") + """COALESCE(s.reads - oldest.reads, 0) > 0
           ORDER BY reads_gained DESC LIMIT 5""",
        sp,
    ).fetchall()

    return {
        "total_submissions": totals["total_submissions"],
        "total_reads": totals["total_reads"],
        "total_votes": totals["total_votes"],
        "total_comments": totals["total_comments"],
        "total_lists": totals["total_lists"],
        "top_read": [dict(r) for r in top_read],
        "top_voted": [dict(r) for r in top_voted],
        "fastest_growing": [dict(r) for r in fastest_growing],
    }


# -- WP Growth Rates ------------------------------------------------------

def _calc_growth_rate(current: int, past: int | None, hours: int) -> float | None:
    if past is None:
        return None
    delta = current - past
    days = hours / 24.0
    return round(delta / days, 2) if days > 0 else None


def get_wp_growth_rates(conn: sqlite3.Connection) -> dict:
    totals = conn.execute(
        "SELECT COALESCE(SUM(reads),0) as reads, COALESCE(SUM(votes),0) as votes, "
        "COALESCE(SUM(comments_count),0) as comments, COALESCE(SUM(num_lists),0) as lists "
        "FROM wp_submissions"
    ).fetchone()
    current_reads = totals["reads"]
    current_votes = totals["votes"]
    current_comments = totals["comments"]
    current_lists = totals["lists"]

    rates = {}
    for label, hours in [("24h", 24), ("7d", 168), ("30d", 720)]:
        row = conn.execute(
            """SELECT SUM(reads) as reads, SUM(votes) as votes,
                      SUM(comments_count) as comments, SUM(num_lists) as lists
               FROM wp_snapshots WHERE polled_at = (
                   SELECT polled_at FROM wp_snapshots
                   WHERE polled_at <= datetime('now', ? || ' hours')
                   ORDER BY polled_at DESC LIMIT 1
               )""",
            (str(-hours),),
        ).fetchone()
        past_reads = row["reads"] if row and row["reads"] is not None else None
        past_votes = row["votes"] if row and row["votes"] is not None else None
        past_comments = row["comments"] if row and row["comments"] is not None else None
        past_lists = row["lists"] if row and row["lists"] is not None else None
        rates[label] = {
            "reads_per_day": _calc_growth_rate(current_reads, past_reads, hours),
            "votes_per_day": _calc_growth_rate(current_votes, past_votes, hours),
            "comments_per_day": _calc_growth_rate(current_comments, past_comments, hours),
            "lists_per_day": _calc_growth_rate(current_lists, past_lists, hours),
        }
    return rates


def get_wp_submission_growth_rates(conn: sqlite3.Connection, submission_id: int) -> dict:
    sub = conn.execute(
        "SELECT reads, votes, comments_count, num_lists FROM wp_submissions WHERE submission_id = ?",
        (submission_id,),
    ).fetchone()
    if not sub:
        return {}

    rates = {}
    for label, hours in [("24h", 24), ("7d", 168), ("30d", 720)]:
        row = conn.execute(
            """SELECT reads, votes, comments_count as comments, num_lists as lists
               FROM wp_snapshots WHERE submission_id = ? AND polled_at <= datetime('now', ? || ' hours')
               ORDER BY polled_at DESC LIMIT 1""",
            (submission_id, str(-hours)),
        ).fetchone()
        past_reads = row["reads"] if row else None
        past_votes = row["votes"] if row else None
        past_comments = row["comments"] if row else None
        past_lists = row["lists"] if row else None
        rates[label] = {
            "reads_per_day": _calc_growth_rate(sub["reads"], past_reads, hours),
            "votes_per_day": _calc_growth_rate(sub["votes"], past_votes, hours),
            "comments_per_day": _calc_growth_rate(sub["comments_count"], past_comments, hours),
            "lists_per_day": _calc_growth_rate(sub["num_lists"], past_lists, hours),
        }
    return rates


def get_wp_submission_deltas(conn: sqlite3.Connection) -> dict[str, dict]:
    rows = conn.execute(
        """SELECT s.submission_id,
                  COALESCE(s.reads - old.reads, 0) as reads_delta,
                  COALESCE(s.votes - old.votes, 0) as votes_delta,
                  COALESCE(s.comments_count - old.comments_count, 0) as comments_delta,
                  COALESCE(s.num_lists - old.num_lists, 0) as lists_delta
           FROM wp_submissions s
           LEFT JOIN (
               SELECT s1.submission_id, s1.reads, s1.votes, s1.comments_count, s1.num_lists
               FROM wp_snapshots s1
               INNER JOIN (
                   SELECT submission_id, MAX(polled_at) as max_polled
                   FROM wp_snapshots
                   WHERE polled_at <= datetime('now', '-24 hours')
                   GROUP BY submission_id
               ) s2 ON s1.submission_id = s2.submission_id AND s1.polled_at = s2.max_polled
           ) old ON s.submission_id = old.submission_id"""
    ).fetchall()
    return {str(r["submission_id"]): dict(r) for r in rows}
