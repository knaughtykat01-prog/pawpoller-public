"""All SQL CRUD functions for the Inkbunny (IB) analytics database.

This module is the primary query layer for the Inkbunny platform. It covers:
- Session cache (singleton pattern for persisting API session tokens)
- Submission CRUD with upsert semantics
- Snapshot time-series (per-submission and aggregate)
- Faving-user tracking (unique to IB -- FA and WS lack this data)
- Comment tracking
- Poll logging for audit trails
- Summary/dashboard statistics with configurable stat offsets
- Growth rate calculations across 24h / 7d / 30d windows
- 24-hour delta calculations for each submission
"""

from __future__ import annotations
import json
from database.scope import account_clause  # optional `account_id = ?` WHERE-injection
import sqlite3
from datetime import datetime, timezone
from typing import Any


# ── Session Cache ──────────────────────────────────────────────
# Multi-account: session_cache holds one row PER ACCOUNT (PK account_id), so two
# Inkbunny accounts can each keep their own cached API session (SID) and resume
# without re-logging in. (Pre-multi-account this was a singleton row id=1.)

def get_cached_session(conn: sqlite3.Connection, account_id: int) -> dict | None:
    try:
        row = conn.execute(
            "SELECT sid, username, user_id, created_at FROM session_cache WHERE account_id = ?",
            (account_id,)).fetchone()
    except Exception:
        row = conn.execute(
            "SELECT sid, username, created_at FROM session_cache WHERE account_id = ?",
            (account_id,)).fetchone()
    return dict(row) if row else None


def save_session(conn: sqlite3.Connection, account_id: int, sid: str,
                 username: str, user_id: int = 0) -> None:
    # Upsert keyed on account_id (the PK) — one cached session per account.
    conn.execute(
        "INSERT INTO session_cache (account_id, sid, username, user_id, created_at)"
        " VALUES (?, ?, ?, ?, datetime('now'))"
        " ON CONFLICT(account_id) DO UPDATE SET sid=excluded.sid, username=excluded.username,"
        " user_id=excluded.user_id, created_at=excluded.created_at",
        (account_id, sid, username, user_id),
    )
    conn.commit()


def clear_session(conn: sqlite3.Connection, account_id: int | None = None) -> None:
    # Wipes cached session data. With account_id, clears just that account;
    # without it, clears every IB account's session (the "log out" button).
    if account_id is None:
        conn.execute("DELETE FROM session_cache")
    else:
        conn.execute("DELETE FROM session_cache WHERE account_id = ?", (account_id,))
    conn.commit()


# ── Submissions ────────────────────────────────────────────────

def upsert_submission(conn: sqlite3.Connection, sub: dict, account_id: int) -> None:
    """Insert or update a submission's metadata and latest stats.

    Uses the SQLite upsert pattern (INSERT ... ON CONFLICT ... DO UPDATE):
    - First poll for a submission_id: row is inserted with all metadata + stats.
    - Subsequent polls: the existing row is updated with the latest metadata and
      stat counts (views, favorites_count, comments_count) plus a fresh updated_at
      timestamp. The create_datetime is intentionally NOT overwritten on update
      because it represents the original post date on Inkbunny.

    Keywords are JSON-serialized because the Inkbunny API returns them as a list
    of objects. Storing as a JSON string in a TEXT column keeps the schema simple
    (single column) while preserving the structured keyword data for later display
    or filtering without needing a separate keywords junction table.
    """
    # Serialize the keyword list to a JSON string for storage in a TEXT column.
    keywords_json = json.dumps(sub.get("keywords", []))
    # account_id is set on INSERT only; the ON CONFLICT UPDATE deliberately does
    # NOT touch it — an existing submission never changes which account owns it.
    conn.execute(
        """INSERT INTO submissions
           (submission_id, account_id, title, username, user_id, create_datetime,
            type_name, rating_id, rating_name, thumb_url, url,
            description, keywords, page_count,
            views, favorites_count, comments_count, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
           ON CONFLICT(submission_id) DO UPDATE SET
            title=excluded.title, username=excluded.username, user_id=excluded.user_id,
            type_name=excluded.type_name, rating_id=excluded.rating_id,
            rating_name=excluded.rating_name, thumb_url=excluded.thumb_url,
            url=excluded.url, description=excluded.description,
            keywords=excluded.keywords, page_count=excluded.page_count,
            views=excluded.views, favorites_count=excluded.favorites_count,
            comments_count=excluded.comments_count, updated_at=datetime('now')
        """,
        (
            sub["submission_id"], account_id, sub.get("title", ""), sub.get("username", ""),
            sub.get("user_id"), sub.get("create_datetime"),
            sub.get("type_name", ""), sub.get("rating_id", 0),
            sub.get("rating_name", ""), sub.get("thumb_url", ""),
            sub.get("url", ""), sub.get("description", ""),
            keywords_json, sub.get("page_count", 1),
            sub.get("views", 0), sub.get("favorites_count", 0),
            sub.get("comments_count", 0),
        ),
    )


def get_submission(conn: sqlite3.Connection, submission_id: int) -> dict | None:
    row = conn.execute("SELECT * FROM submissions WHERE submission_id = ?", (submission_id,)).fetchone()
    return dict(row) if row else None


def get_previous_favorites_count(conn: sqlite3.Connection, submission_id: int) -> int | None:
    """Get the favorites_count from the most recent snapshot for this submission.

    Used by the poller to detect new faves: if the current API count exceeds
    the last-snapshotted count, the poller knows to fetch the faving-users list.
    """
    row = conn.execute(
        "SELECT favorites_count FROM snapshots WHERE submission_id = ? ORDER BY polled_at DESC LIMIT 1",
        (submission_id,),
    ).fetchone()
    return row["favorites_count"] if row else None


def get_all_submissions(conn: sqlite3.Connection, sort_by: str = "views", order: str = "desc", account_id: int | None = None) -> list[dict]:
    # Whitelist of allowed sort columns prevents SQL injection when
    # interpolating the column name into the ORDER BY clause.
    allowed_sorts = {"views", "favorites_count", "comments_count", "title", "create_datetime", "updated_at"}
    if sort_by not in allowed_sorts:
        sort_by = "views"
    order_dir = "DESC" if order.lower() == "desc" else "ASC"
    where, params = account_clause(account_id)
    sql = "SELECT * FROM submissions" + (f" WHERE {where}" if where else "")
    sql += f" ORDER BY {sort_by} {order_dir}"
    rows = conn.execute(sql, params).fetchall()
    return [dict(r) for r in rows]


# ── Snapshots ──────────────────────────────────────────────────
# Snapshots form a time-series of stat readings for each submission. Every poll
# cycle inserts one snapshot row per submission, recording the views, favorites,
# and comments counts at that moment. This append-only log enables:
#   - Per-submission trend charts (get_snapshots)
#   - Portfolio-wide aggregate charts (get_aggregate_snapshots)
#   - Growth rate calculations by comparing current vs. past snapshots
#   - 24-hour delta calculations for the dashboard

def insert_snapshot(conn: sqlite3.Connection, account_id: int, submission_id: int, views: int, favorites_count: int, comments_count: int, polled_at: str | None = None) -> None:
    # Each poll cycle appends a new snapshot row -- never updates existing ones.
    # The polled_at timestamp defaults to UTC now, but can be overridden for
    # historical imports or testing.
    ts = polled_at or datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    conn.execute(
        "INSERT INTO snapshots (account_id, submission_id, polled_at, views, favorites_count, comments_count) VALUES (?, ?, ?, ?, ?, ?)",
        (account_id, submission_id, ts, views, favorites_count, comments_count),
    )


def get_snapshots(conn: sqlite3.Connection, submission_id: int, start: str | None = None, end: str | None = None) -> list[dict]:
    """Per-submission time-series: returns all snapshot rows for one submission.

    Optional start/end parameters allow filtering to a date range, used by the
    charts UI to zoom into a specific time window. Results are ordered
    chronologically (ASC) for direct use as chart data points.
    """
    sql = "SELECT * FROM snapshots WHERE submission_id = ?"
    params: list[Any] = [submission_id]
    if start:
        sql += " AND polled_at >= ?"
        params.append(start)
    if end:
        sql += " AND polled_at <= ?"
        params.append(end)
    sql += " ORDER BY polled_at ASC"
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def get_aggregate_snapshots(conn: sqlite3.Connection, start: str | None = None, end: str | None = None, account_id: int | None = None) -> list[dict]:
    """Aggregate time-series: sum of views/faves/comments across ALL submissions per poll timestamp.

    Because all submissions are polled at the same time, GROUP BY polled_at
    produces one row per poll cycle with portfolio-wide totals. This powers
    the "all submissions" aggregate chart on the dashboard. With *account_id*
    set, the totals are scoped to that account.
    """
    sql = "SELECT polled_at, SUM(views) as views, SUM(favorites_count) as favorites_count, SUM(comments_count) as comments_count FROM snapshots"
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


def get_comparison_snapshots(conn: sqlite3.Connection, submission_ids: list[int], start: str | None = None, end: str | None = None) -> dict[int, list[dict]]:
    """Get snapshot time-series for multiple submissions, keyed by submission_id.

    Used by the comparison chart view to overlay multiple submissions' trends
    on the same axes. Returns a dict so the frontend can map each series to
    its submission metadata. One IN-clause query for all submissions instead
    of N per-submission SELECTs (was a per-platform N+1 hot path on the
    comparison chart).
    """
    result: dict[int, list[dict]] = {sid: [] for sid in submission_ids}
    if not submission_ids:
        return result
    placeholders = ",".join("?" * len(submission_ids))
    sql = f"SELECT * FROM snapshots WHERE submission_id IN ({placeholders})"
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


# ── Faving Users ───────────────────────────────────────────────
# Inkbunny is the only platform that exposes a per-submission list of users
# who have favorited a submission. FA and WS only provide aggregate counts.
# This table tracks each individual fave event for the "recent faves" feed
# and the "top fans" leaderboard in analytics.

def upsert_faving_user(conn: sqlite3.Connection, submission_id: int, user_id: int, username: str) -> bool:
    """Insert a faving user if not already tracked. Returns True if new.

    Uses INSERT with a UNIQUE constraint on (submission_id, user_id) to
    deduplicate. If the user already faved this submission, the IntegrityError
    is caught and False is returned -- no UPDATE is needed because fave records
    are immutable (we only care about first_seen_at).
    """
    try:
        conn.execute(
            "INSERT INTO faving_users (submission_id, user_id, username, first_seen_at) VALUES (?, ?, ?, datetime('now'))",
            (submission_id, user_id, username),
        )
        return True
    except sqlite3.IntegrityError:
        # Already tracked -- this user previously faved this submission.
        return False


def upsert_faving_users_batch(conn: sqlite3.Connection, account_id: int, submission_id: int, users: list[dict]) -> int:
    """Batch insert faving users. Returns count of new faves."""
    if not users:
        return 0
    before = conn.execute("SELECT COUNT(*) FROM faving_users WHERE submission_id = ?", (submission_id,)).fetchone()[0]
    conn.executemany(
        "INSERT OR IGNORE INTO faving_users (account_id, submission_id, user_id, username, first_seen_at) VALUES (?, ?, ?, ?, datetime('now'))",
        [(account_id, submission_id, u["user_id"], u["username"]) for u in users],
    )
    after = conn.execute("SELECT COUNT(*) FROM faving_users WHERE submission_id = ?", (submission_id,)).fetchone()[0]
    return after - before


def get_faving_users(conn: sqlite3.Connection, submission_id: int) -> list[dict]:
    # Returns all users who faved a specific submission, newest first.
    rows = conn.execute(
        "SELECT * FROM faving_users WHERE submission_id = ? ORDER BY first_seen_at DESC",
        (submission_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def get_recent_faves(conn: sqlite3.Connection, limit: int = 20, account_id: int | None = None) -> list[dict]:
    """Most recently detected faves across all submissions.

    Joins to submissions to include the submission title for display in the
    dashboard's "recent activity" feed.
    """
    where, params = account_clause(account_id, "f")
    sql = ("SELECT f.*, s.title as submission_title"
           " FROM faving_users f"
           " JOIN submissions s ON f.submission_id = s.submission_id")
    if where:
        sql += " WHERE " + where
    sql += " ORDER BY f.first_seen_at DESC LIMIT ?"
    params.append(limit)
    rows = conn.execute(sql, params).fetchall()
    return [dict(r) for r in rows]


# ── Comments ──────────────────────────────────────────────────
# Inkbunny comments are tracked individually, similar to faving_users above.
# Each comment has a unique comment_id from the API, used for deduplication.

def upsert_comment(conn: sqlite3.Connection, comment: dict,
                   is_own: bool = False) -> bool:
    """Insert a comment if not already tracked. Returns True if new.

    Same deduplication strategy as upsert_faving_user: rely on the UNIQUE
    constraint on comment_id and catch IntegrityError for duplicates.
    Comments are immutable once inserted -- we never update their text.

    *is_own* flags a comment written by the posting account (2.192.0) so it is
    excluded from new-comment counts, notifications and Top Fans while staying
    readable as thread context.
    """
    try:
        conn.execute(
            """INSERT INTO comments (comment_id, submission_id, username, comment_text,
               commented_at, first_seen_at, is_reply, reply_to_comment_id, is_own)
               VALUES (?, ?, ?, ?, ?, datetime('now'), ?, ?, ?)""",
            (
                comment["comment_id"], comment["submission_id"], comment.get("username", ""),
                comment.get("comment_text", ""), comment.get("commented_at"),
                1 if comment.get("is_reply") else 0, comment.get("reply_to_comment_id"),
                1 if is_own else 0,
            ),
        )
        return True
    except sqlite3.IntegrityError:
        return False


def get_comments(conn: sqlite3.Connection, submission_id: int) -> list[dict]:
    # Ordered by comment_id ASC to preserve chronological conversation order.
    rows = conn.execute(
        "SELECT * FROM comments WHERE submission_id = ? ORDER BY comment_id ASC",
        (submission_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def get_recent_comments(conn: sqlite3.Connection, limit: int = 20, account_id: int | None = None) -> list[dict]:
    """Most recently detected comments across all submissions.

    Joins to submissions for display context in the dashboard activity feed.
    Ordered by first_seen_at (when our poller discovered the comment), not
    commented_at (when the user actually posted it), because first_seen_at
    reflects the user's real-time notification experience.
    """
    where, params = account_clause(account_id, "c")
    sql = ("SELECT c.*, s.title as submission_title"
           " FROM comments c"
           " JOIN submissions s ON c.submission_id = s.submission_id")
    # Our own comments are not activity to report back to us (2.192.0).
    where = (where + " AND " if where else "") + "COALESCE(c.is_own, 0) = 0"
    sql += " WHERE " + where
    sql += " ORDER BY c.first_seen_at DESC LIMIT ?"
    params.append(limit)
    rows = conn.execute(sql, params).fetchall()
    return [dict(r) for r in rows]


def get_previous_comments_count(conn: sqlite3.Connection, submission_id: int) -> int | None:
    """Get the comments_count from the most recent snapshot for this submission.

    Used by the poller to detect new comments: if the current API count exceeds
    this value, the poller fetches the full comment list to find new entries.
    """
    row = conn.execute(
        "SELECT comments_count FROM snapshots WHERE submission_id = ? ORDER BY polled_at DESC LIMIT 1",
        (submission_id,),
    ).fetchone()
    return row["comments_count"] if row else None


# ── Poll Log ──────────────────────────────────────────────────
# The poll_log table records every poll cycle for auditing and diagnostics.
# Each cycle creates a row at start (status='running'), then updates it on
# completion with final stats and timing info.

def start_poll_log(conn: sqlite3.Connection, account_id: int = 0) -> int:
    # Creates a new poll_log entry with status 'running'. Returns the row ID
    # so finish_poll_log can update the same row when the cycle completes.
    cur = conn.execute(
        "INSERT INTO poll_log (started_at, status, account_id) VALUES (datetime('now'), 'running', ?)",
        (account_id,))
    conn.commit()
    return cur.lastrowid


def finish_poll_log(conn: sqlite3.Connection, log_id: int, status: str, submissions_found: int = 0,
                    snapshots_inserted: int = 0, new_faves_found: int = 0,
                    error_message: str | None = None, duration_seconds: float = 0,
                    new_comments_found: int = 0, new_watchers_found: int = 0) -> None:
    # Updates the poll_log row created by start_poll_log with final results.
    # Status is typically 'success' or 'error'. On error, error_message
    # captures the exception details for debugging.
    conn.execute(
        """UPDATE poll_log SET finished_at=datetime('now'), status=?, submissions_found=?,
           snapshots_inserted=?, new_faves_found=?, new_comments_found=?, new_watchers_found=?, error_message=?, duration_seconds=?
           WHERE id=?""",
        (status, submissions_found, snapshots_inserted, new_faves_found, new_comments_found, new_watchers_found, error_message, duration_seconds, log_id),
    )
    conn.commit()


def get_last_poll(conn: sqlite3.Connection) -> dict | None:
    # Returns the most recent poll cycle entry, used by the dashboard to show
    # "last polled at" status.
    row = conn.execute("SELECT * FROM poll_log ORDER BY started_at DESC LIMIT 1").fetchone()
    return dict(row) if row else None


def get_poll_log(conn: sqlite3.Connection, limit: int = 50) -> list[dict]:
    # Returns recent poll history for the admin/diagnostics view.
    rows = conn.execute("SELECT * FROM poll_log ORDER BY started_at DESC LIMIT ?", (limit,)).fetchall()
    return [dict(r) for r in rows]


# ── Summary Stats ─────────────────────────────────────────────

def get_summary(conn: sqlite3.Connection, account_id: int | None = None) -> dict:
    """Main dashboard data source -- assembles all the summary stats for the IB dashboard.

    This is the single entry point that the dashboard route calls to populate
    the entire overview page. It returns:
    - Aggregate totals (with offset corrections)
    - Top 5 most-viewed submissions
    - Top 5 most-faved submissions
    - Top 5 fastest-growing submissions (by 24h view increase)
    - 10 most recent fave events
    - 10 most recent comment events

    With *account_id* set, every total/top-list is scoped to that account; the
    "All accounts" default (account_id=None) keeps the aggregate behaviour.
    """
    import config
    where, wp = account_clause(account_id)
    w = f" WHERE {where}" if where else ""
    totals = conn.execute(
        "SELECT COUNT(*) as total_submissions, COALESCE(SUM(views),0) as total_views, "
        "COALESCE(SUM(favorites_count),0) as total_favorites, COALESCE(SUM(comments_count),0) as total_comments "
        "FROM submissions" + w,
        wp,
    ).fetchone()

    # Apply offset constants from config to account for stats from submissions
    # that have been deleted or made private and are no longer visible via the
    # Inkbunny API. These offsets are a property of the default account's
    # historical aggregate, so only apply them to the "All accounts" view.
    totals = dict(totals)
    if account_id is None:
        totals["total_views"] += config.VIEWS_OFFSET
        totals["total_favorites"] += config.FAVORITES_OFFSET
        totals["total_comments"] += config.COMMENTS_OFFSET

    top_viewed = conn.execute(
        "SELECT submission_id, title, views, thumb_url FROM submissions" + w + " ORDER BY views DESC LIMIT 5",
        wp,
    ).fetchall()

    top_faved = conn.execute(
        "SELECT submission_id, title, favorites_count, thumb_url FROM submissions" + w + " ORDER BY favorites_count DESC LIMIT 5",
        wp,
    ).fetchall()

    # Fastest-growing: finds submissions with the biggest view increase in the
    # last 24 hours. The subquery (aliased "oldest") finds the nearest snapshot
    # that is at least 24 hours old for each submission. Only the outer `s` needs
    # account scoping — submission_ids are unique to their account, so the
    # snapshot join is implicitly account-correct.
    sw, sp = account_clause(account_id, "s")
    fastest_growing = conn.execute(
        """SELECT s.submission_id, s.title, s.thumb_url,
                  COALESCE(s.views - oldest.views, 0) as views_gained,
                  COALESCE(s.favorites_count - oldest.favorites_count, 0) as faves_gained
           FROM submissions s
           LEFT JOIN (
               SELECT s1.submission_id, s1.views, s1.favorites_count
               FROM snapshots s1
               INNER JOIN (
                   SELECT submission_id, MAX(polled_at) as max_polled
                   FROM snapshots
                   WHERE polled_at <= datetime('now', '-24 hours')
                   GROUP BY submission_id
               ) s2 ON s1.submission_id = s2.submission_id AND s1.polled_at = s2.max_polled
           ) oldest ON s.submission_id = oldest.submission_id
           WHERE """ + (sw + " AND " if sw else "") + """COALESCE(s.views - oldest.views, 0) > 0
           ORDER BY views_gained DESC LIMIT 5""",
        sp,
    ).fetchall()

    recent_faves = get_recent_faves(conn, limit=10, account_id=account_id)
    recent_comments = get_recent_comments(conn, limit=10, account_id=account_id)

    return {
        "total_submissions": totals["total_submissions"],
        "total_views": totals["total_views"],
        "total_favorites": totals["total_favorites"],
        "total_comments": totals["total_comments"],
        "top_viewed": [dict(r) for r in top_viewed],
        "top_faved": [dict(r) for r in top_faved],
        "fastest_growing": [dict(r) for r in fastest_growing],
        "recent_faves": recent_faves,
        "recent_comments": recent_comments,
    }


def _calc_growth_rate(current: int, past: int | None, hours: int) -> float | None:
    """Calculate a normalised daily growth rate from a past snapshot value.

    Formula: daily_rate = (current - past) / (hours / 24)
    This normalises the raw delta to a per-day rate regardless of the time
    window. For example, if views grew by 700 over 168 hours (7 days),
    the daily rate is 700 / 7 = 100 views/day.

    Returns None if there is no past data to compare against.
    """
    if past is None:
        return None
    delta = current - past
    days = hours / 24.0
    return round(delta / days, 2) if days > 0 else None


def get_growth_rates(conn: sqlite3.Connection) -> dict:
    """Aggregate growth rates across all submissions for 24h, 7d, 30d.

    For each time window, the function:
    1. Takes current totals from the submissions table (plus offsets).
    2. Finds the nearest past snapshot: the inner subquery selects the single
       polled_at timestamp that is closest to (but not after) the target
       boundary (e.g. 24 hours ago). The outer query then sums all snapshot
       values at that exact timestamp to get the portfolio-wide past totals.
    3. Applies _calc_growth_rate to compute the normalised daily rate.

    The offset constants are added to both current and past values so the
    growth rate reflects the true portfolio including deleted/private subs.
    """
    import config
    totals = conn.execute(
        "SELECT COALESCE(SUM(views),0) as views, COALESCE(SUM(favorites_count),0) as faves, "
        "COALESCE(SUM(comments_count),0) as comments FROM submissions"
    ).fetchone()
    # Current values include offsets for deleted/private submissions.
    current_views = totals["views"] + config.VIEWS_OFFSET
    current_faves = totals["faves"] + config.FAVORITES_OFFSET
    current_comments = totals["comments"] + config.COMMENTS_OFFSET

    rates = {}
    for label, hours in [("24h", 24), ("7d", 168), ("30d", 720)]:
        # Find the snapshot timestamp nearest to (now - hours). The inner
        # SELECT finds the single closest polled_at that is at or before the
        # boundary. The outer SELECT then sums all snapshot metrics at that
        # exact timestamp across all submissions.
        row = conn.execute(
            """SELECT SUM(views) as views, SUM(favorites_count) as faves, SUM(comments_count) as comments
               FROM snapshots WHERE polled_at = (
                   SELECT polled_at FROM snapshots
                   WHERE polled_at <= datetime('now', ? || ' hours')
                   ORDER BY polled_at DESC LIMIT 1
               )""",
            (str(-hours),),
        ).fetchone()
        # Past values also need offsets so the delta calculation is consistent.
        past_views = (row["views"] + config.VIEWS_OFFSET) if row and row["views"] is not None else None
        past_faves = (row["faves"] + config.FAVORITES_OFFSET) if row and row["faves"] is not None else None
        past_comments = (row["comments"] + config.COMMENTS_OFFSET) if row and row["comments"] is not None else None
        rates[label] = {
            "views_per_day": _calc_growth_rate(current_views, past_views, hours),
            "faves_per_day": _calc_growth_rate(current_faves, past_faves, hours),
            "comments_per_day": _calc_growth_rate(current_comments, past_comments, hours),
        }
    return rates


def get_submission_growth_rates(conn: sqlite3.Connection, submission_id: int) -> dict:
    """Per-submission growth rates for 24h, 7d, 30d.

    Same approach as get_growth_rates but scoped to a single submission.
    No offsets are applied here because offsets only apply to portfolio-wide
    totals (deleted submissions), not individual submission stats.

    For each time window, finds the nearest snapshot at or before the boundary
    timestamp and computes the normalised daily growth rate.
    """
    sub = conn.execute(
        "SELECT views, favorites_count, comments_count FROM submissions WHERE submission_id = ?",
        (submission_id,),
    ).fetchone()
    if not sub:
        return {}

    rates = {}
    for label, hours in [("24h", 24), ("7d", 168), ("30d", 720)]:
        # Find the snapshot closest to the time boundary for this submission.
        # ORDER BY polled_at DESC LIMIT 1 gives us the newest snapshot that
        # is still at least `hours` hours old.
        row = conn.execute(
            """SELECT views, favorites_count as faves, comments_count as comments
               FROM snapshots WHERE submission_id = ? AND polled_at <= datetime('now', ? || ' hours')
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


def get_submission_deltas(conn: sqlite3.Connection) -> dict[int, dict]:
    """Get 24h deltas (change in views/faves/comments) for every submission.

    Uses a LEFT JOIN subquery to find each submission's snapshot from ~24h ago:
    - The subquery filters snapshots to those at least 24 hours old, groups by
      submission_id, and uses HAVING polled_at = MAX(polled_at) to pick the
      nearest snapshot to the 24h boundary for each submission.
    - LEFT JOIN ensures submissions without a 24h-old snapshot still appear
      in the result set (their deltas default to 0 via COALESCE).
    - The delta is computed as: current value (from submissions table) minus
      the past value (from the matched snapshot).

    Returns a dict keyed by submission_id for O(1) lookup by the caller.
    """
    rows = conn.execute(
        """SELECT s.submission_id,
                  COALESCE(s.views - old.views, 0) as views_delta,
                  COALESCE(s.favorites_count - old.favorites_count, 0) as faves_delta,
                  COALESCE(s.comments_count - old.comments_count, 0) as comments_delta
           FROM submissions s
           LEFT JOIN (
               SELECT s1.submission_id, s1.views, s1.favorites_count, s1.comments_count
               FROM snapshots s1
               INNER JOIN (
                   SELECT submission_id, MAX(polled_at) as max_polled
                   FROM snapshots
                   WHERE polled_at <= datetime('now', '-24 hours')
                   GROUP BY submission_id
               ) s2 ON s1.submission_id = s2.submission_id AND s1.polled_at = s2.max_polled
           ) old ON s.submission_id = old.submission_id"""
    ).fetchall()
    return {r["submission_id"]: dict(r) for r in rows}


# ── Watcher Queries ───────────────────────────────────────────────

def upsert_watcher(conn: sqlite3.Connection, account_id: int, username: str) -> bool:
    """Insert a watcher if not already tracked for this account. Returns True if new.

    Keyed on (account_id, username) because Inkbunny's usersviewall page does not
    expose user_id values in its HTML, and two accounts can share a watcher.
    """
    row = conn.execute(
        "SELECT 1 FROM watchers WHERE account_id = ? AND username = ?", (account_id, username)
    ).fetchone()
    if row:
        return False
    conn.execute(
        "INSERT INTO watchers (account_id, user_id, username, first_seen_at) VALUES (?, 0, ?, datetime('now'))",
        (account_id, username),
    )
    return True


def remove_stale_watchers(conn: sqlite3.Connection, account_id: int, current_usernames: list[str]) -> int:
    """Remove this account's watchers no longer on the live watcher list.

    Inkbunny's watched_by page only shows active watchers — banned, deleted,
    and unwatched accounts disappear. This prunes the DB to match reality, scoped
    to one account so it never touches another account's watchers.
    Returns the number of rows deleted.
    """
    if not current_usernames:
        return 0
    placeholders = ",".join("?" for _ in current_usernames)
    cur = conn.execute(
        f"DELETE FROM watchers WHERE account_id = ? AND username NOT IN ({placeholders})",
        [account_id, *current_usernames],
    )
    return cur.rowcount


def get_watchers_count(conn: sqlite3.Connection) -> int:
    """Total number of tracked watchers."""
    row = conn.execute("SELECT COUNT(*) as c FROM watchers").fetchone()
    return row["c"] if row else 0


def get_recent_watchers(conn: sqlite3.Connection, limit: int = 20) -> list[dict]:
    """Most recent watchers, newest first."""
    rows = conn.execute(
        "SELECT * FROM watchers ORDER BY first_seen_at DESC LIMIT ?",
        (limit,),
    ).fetchall()
    return [dict(r) for r in rows]
