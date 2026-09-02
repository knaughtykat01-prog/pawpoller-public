"""All SQL CRUD functions for the FurAffinity (FA) analytics database.

This module mirrors the structure of queries.py (Inkbunny) but operates on
fa_-prefixed tables. Key differences from the Inkbunny module:
  - NO faving_users tracking: FA does not expose a per-submission list of
    users who favorited a submission, so there is no faving_users equivalent.
    Only aggregate favorites_count is available from scraping.
  - HAS individual comment tracking: FA comments are scraped and stored in
    fa_comments, with slightly different fields (reply_to, reply_level,
    is_deleted) reflecting FA's nested comment structure.
  - NO stat offsets: FA summary totals are not adjusted with offsets (unlike
    IB which uses VIEWS_OFFSET, FAVORITES_OFFSET, COMMENTS_OFFSET to account
    for deleted/private submissions).
  - Different metadata columns: FA submissions have category, theme, species,
    gender fields instead of IB's type_name, rating_id, page_count.
  - Comment IDs are stored as strings (FA uses non-numeric comment IDs).
"""

from __future__ import annotations
import json
from database.scope import account_clause  # optional `account_id = ?` WHERE-injection
import sqlite3
from datetime import datetime, timezone
from typing import Any


# ── FA Submissions ────────────────────────────────────────────

def upsert_fa_submission(conn: sqlite3.Connection, sub: dict, account_id: int) -> None:
    """Insert or update an FA submission's metadata and latest stats.

    Same upsert pattern as queries.upsert_submission: INSERT with ON CONFLICT
    UPDATE. Keywords are JSON-serialized to a TEXT column for the same reasons
    as in the IB module (preserves structured data without a junction table).

    FA-specific columns include category, theme, species, gender, and a
    direct link URL (FA submissions are scraped, not fetched via a REST API,
    so the link field stores the full FA submission URL).
    """
    # Serialize keywords list to JSON string, same pattern as IB.
    keywords_json = json.dumps(sub.get("keywords", []))
    # account_id is set on INSERT only; the ON CONFLICT UPDATE leaves it alone.
    conn.execute(
        """INSERT INTO fa_submissions
           (submission_id, account_id, title, username, posted_at, category, theme,
            species, gender, rating, thumbnail_url, download_url,
            description, keywords, link,
            views, favorites_count, comments_count, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
           ON CONFLICT(submission_id) DO UPDATE SET
            title=excluded.title, username=excluded.username,
            category=excluded.category, theme=excluded.theme,
            species=excluded.species, gender=excluded.gender,
            rating=excluded.rating, thumbnail_url=excluded.thumbnail_url,
            download_url=excluded.download_url, description=excluded.description,
            keywords=excluded.keywords, link=excluded.link,
            views=excluded.views, favorites_count=excluded.favorites_count,
            comments_count=excluded.comments_count, updated_at=datetime('now')
        """,
        (
            sub["submission_id"], account_id, sub.get("title", ""), sub.get("username", ""),
            sub.get("posted_at"), sub.get("category", ""), sub.get("theme", ""),
            sub.get("species", ""), sub.get("gender", ""), sub.get("rating", ""),
            sub.get("thumbnail_url", ""), sub.get("download_url", ""),
            sub.get("description", ""), keywords_json, sub.get("link", ""),
            sub.get("views", 0), sub.get("favorites_count", 0),
            sub.get("comments_count", 0),
        ),
    )


def get_fa_submission(conn: sqlite3.Connection, submission_id: int) -> dict | None:
    row = conn.execute("SELECT * FROM fa_submissions WHERE submission_id = ?", (submission_id,)).fetchone()
    return dict(row) if row else None


def get_fa_previous_favorites_count(conn: sqlite3.Connection, submission_id: int) -> int | None:
    """Get the favorites_count from the most recent FA snapshot for change detection.

    Unlike IB, this is only used for detecting count changes (to know when to
    re-scrape), NOT for identifying individual users who faved -- FA does not
    expose that data.
    """
    row = conn.execute(
        "SELECT favorites_count FROM fa_snapshots WHERE submission_id = ? ORDER BY polled_at DESC LIMIT 1",
        (submission_id,),
    ).fetchone()
    return row["favorites_count"] if row else None


def get_all_fa_submissions(conn: sqlite3.Connection, sort_by: str = "views", order: str = "desc", account_id: int | None = None) -> list[dict]:
    # Whitelist-based sort column validation, same pattern as IB.
    # Uses posted_at instead of create_datetime (FA terminology difference).
    allowed_sorts = {"views", "favorites_count", "comments_count", "title", "posted_at", "updated_at"}
    if sort_by not in allowed_sorts:
        sort_by = "views"
    order_dir = "DESC" if order.lower() == "desc" else "ASC"
    where, params = account_clause(account_id)
    sql = "SELECT * FROM fa_submissions" + (f" WHERE {where}" if where else "")
    sql += f" ORDER BY {sort_by} {order_dir}"
    rows = conn.execute(sql, params).fetchall()
    return [dict(r) for r in rows]


# ── FA Snapshots ──────────────────────────────────────────────
# Snapshot time-series for FA submissions. Same append-only pattern as IB
# snapshots -- one row per submission per poll cycle.

def insert_fa_snapshot(conn: sqlite3.Connection, account_id: int, submission_id: int, views: int, favorites_count: int, comments_count: int, polled_at: str | None = None) -> None:
    # Append-only: each poll cycle adds a new row, never updates existing ones.
    ts = polled_at or datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    conn.execute(
        "INSERT INTO fa_snapshots (account_id, submission_id, polled_at, views, favorites_count, comments_count) VALUES (?, ?, ?, ?, ?, ?)",
        (account_id, submission_id, ts, views, favorites_count, comments_count),
    )


def get_fa_snapshots(conn: sqlite3.Connection, submission_id: int, start: str | None = None, end: str | None = None) -> list[dict]:
    """Per-submission time-series with optional date range filtering.
    Mirrors queries.get_snapshots for the fa_snapshots table."""
    sql = "SELECT * FROM fa_snapshots WHERE submission_id = ?"
    params: list[Any] = [submission_id]
    if start:
        sql += " AND polled_at >= ?"
        params.append(start)
    if end:
        sql += " AND polled_at <= ?"
        params.append(end)
    sql += " ORDER BY polled_at ASC"
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def get_fa_aggregate_snapshots(conn: sqlite3.Connection, start: str | None = None, end: str | None = None, account_id: int | None = None) -> list[dict]:
    """Aggregate time-series across all FA submissions per poll timestamp.
    Mirrors queries.get_aggregate_snapshots for the fa_snapshots table. With
    *account_id* set, the totals are scoped to that account."""
    sql = "SELECT polled_at, SUM(views) as views, SUM(favorites_count) as favorites_count, SUM(comments_count) as comments_count FROM fa_snapshots"
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


def get_fa_comparison_snapshots(conn: sqlite3.Connection, submission_ids: list[int], start: str | None = None, end: str | None = None) -> dict[int, list[dict]]:
    """Multi-submission time-series for comparison charts. One IN-clause query."""
    result: dict[int, list[dict]] = {sid: [] for sid in submission_ids}
    if not submission_ids:
        return result
    placeholders = ",".join("?" * len(submission_ids))
    sql = f"SELECT * FROM fa_snapshots WHERE submission_id IN ({placeholders})"
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


# ── FA Comments ───────────────────────────────────────────────
# FA has individual comment tracking (like IB) but with slightly different
# schema: reply_to (parent comment ID), reply_level (nesting depth), and
# is_deleted (FA shows deleted comment placeholders). IB uses is_reply
# (boolean) and reply_to_comment_id instead.
# Note: comment_id is stored as a string (str()) because FA comment IDs
# from scraping may not be purely numeric.

def upsert_fa_comment(conn: sqlite3.Connection, comment: dict, account_id: int = 0) -> bool:
    """Insert an FA comment if not already tracked. Returns True if new.

    Same deduplication pattern as IB: rely on UNIQUE constraint + catch
    IntegrityError. FA comments include reply_level for nested threading
    and is_deleted to track removed comments (FA shows "[comment deleted]"
    placeholders that IB does not).
    """
    try:
        conn.execute(
            """INSERT INTO fa_comments (comment_id, account_id, submission_id, username, comment_text,
               commented_at, first_seen_at, reply_to, reply_level, is_deleted)
               VALUES (?, ?, ?, ?, ?, ?, datetime('now'), ?, ?, ?)""",
            (
                # FA comment IDs are cast to string because they may come from
                # scraping in non-integer formats.
                str(comment["comment_id"]), account_id, comment["submission_id"], comment.get("username", ""),
                comment.get("comment_text", ""), comment.get("commented_at"),
                comment.get("reply_to"), comment.get("reply_level", 0),
                1 if comment.get("is_deleted") else 0,
            ),
        )
        return True
    except sqlite3.IntegrityError:
        return False


def upsert_fa_comments_batch(conn: sqlite3.Connection, account_id: int, comments: list[dict],
                             own_handles: set[str] | None = None) -> int:
    """Batch insert FA comments. Returns count of new comments.

    *own_handles* (2.192.0) is the set of normalised handles that mean "us"; a
    matching comment is stored with is_own = 1 so it never counts as new
    engagement. The returned count still reflects rows inserted — callers that
    report "new comments" filter on is_own themselves, because
    ``total_changes`` cannot distinguish which rows were ours.
    """
    if not comments:
        return 0
    from polling import self_comment

    before = conn.total_changes
    conn.executemany(
        """INSERT OR IGNORE INTO fa_comments (comment_id, account_id, submission_id, username, comment_text,
           commented_at, first_seen_at, reply_to, reply_level, is_deleted, is_own)
           VALUES (?, ?, ?, ?, ?, ?, datetime('now'), ?, ?, ?, ?)""",
        [(str(c["comment_id"]), account_id, c["submission_id"], c.get("username", ""),
          c.get("comment_text", ""), c.get("commented_at"),
          c.get("reply_to"), c.get("reply_level", 0),
          1 if c.get("is_deleted") else 0,
          1 if self_comment.is_own_author(c.get("username", ""), own_handles or set()) else 0)
         for c in comments],
    )
    return conn.total_changes - before


def get_fa_comments(conn: sqlite3.Connection, submission_id: int) -> list[dict]:
    # Ordered by first_seen_at then comment_id to preserve chronological order.
    # comment_id is TEXT in FA, so lexicographic sorting would be incorrect.
    rows = conn.execute(
        "SELECT * FROM fa_comments WHERE submission_id = ? ORDER BY first_seen_at ASC, comment_id ASC",
        (submission_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def get_fa_recent_comments(conn: sqlite3.Connection, limit: int = 20, account_id: int | None = None) -> list[dict]:
    """Most recently detected FA comments, joined with submission titles for display.
    With *account_id* set, comments are scoped to that account."""
    where, params = account_clause(account_id, "c")
    sql = ("SELECT c.*, s.title as submission_title"
           " FROM fa_comments c"
           " JOIN fa_submissions s ON c.submission_id = s.submission_id")
    # Our own comments are not activity to report back to us (2.192.0).
    where = (where + " AND " if where else "") + "COALESCE(c.is_own, 0) = 0"
    sql += " WHERE " + where
    sql += " ORDER BY c.first_seen_at DESC LIMIT ?"
    params.append(limit)
    rows = conn.execute(sql, params).fetchall()
    return [dict(r) for r in rows]


def get_fa_previous_comments_count(conn: sqlite3.Connection, submission_id: int) -> int | None:
    """Get the comments_count from the most recent FA snapshot.

    Used by the poller to detect new comments by comparing the current
    scraped count against the last-snapshotted count.
    """
    row = conn.execute(
        "SELECT comments_count FROM fa_snapshots WHERE submission_id = ? ORDER BY polled_at DESC LIMIT 1",
        (submission_id,),
    ).fetchone()
    return row["comments_count"] if row else None


# ── FA Poll Log ───────────────────────────────────────────────
# Same poll audit logging pattern as IB. Note: FA poll log tracks
# new_comments_found but NOT new_faves_found (since FA lacks individual
# fave user tracking -- unlike IB's poll log which includes both).

def start_fa_poll_log(conn: sqlite3.Connection, account_id: int = 0) -> int:
    cur = conn.execute(
        "INSERT INTO fa_poll_log (started_at, status, account_id) VALUES (datetime('now'), 'running', ?)",
        (account_id,))
    conn.commit()
    return cur.lastrowid


def finish_fa_poll_log(conn: sqlite3.Connection, log_id: int, status: str, submissions_found: int = 0,
                       snapshots_inserted: int = 0, new_comments_found: int = 0,
                       error_message: str | None = None, duration_seconds: float = 0,
                       new_watchers_found: int = 0) -> None:
    # No new_faves_found parameter here -- FA cannot track individual fave events.
    conn.execute(
        """UPDATE fa_poll_log SET finished_at=datetime('now'), status=?, submissions_found=?,
           snapshots_inserted=?, new_comments_found=?, new_watchers_found=?, error_message=?, duration_seconds=?
           WHERE id=?""",
        (status, submissions_found, snapshots_inserted, new_comments_found, new_watchers_found, error_message, duration_seconds, log_id),
    )
    conn.commit()


def get_fa_last_poll(conn: sqlite3.Connection) -> dict | None:
    row = conn.execute("SELECT * FROM fa_poll_log ORDER BY started_at DESC LIMIT 1").fetchone()
    return dict(row) if row else None


def get_fa_poll_log(conn: sqlite3.Connection, limit: int = 50) -> list[dict]:
    rows = conn.execute("SELECT * FROM fa_poll_log ORDER BY started_at DESC LIMIT ?", (limit,)).fetchall()
    return [dict(r) for r in rows]


# ── FA Summary Stats ──────────────────────────────────────────

def get_fa_summary(conn: sqlite3.Connection, account_id: int | None = None) -> dict:
    """Main dashboard data source for FA -- mirrors queries.get_summary.

    Key differences from the IB version:
    - NO stat offsets applied (FA has no config.VIEWS_OFFSET etc.)
    - NO recent_faves returned (FA lacks individual fave user tracking)
    - Only recent_comments are included in the activity feed
    - thumbnail_url is aliased to thumb_url for frontend consistency with IB

    With *account_id* set, every total/top-list is scoped to that account; the
    "All accounts" default (account_id=None) keeps the aggregate behaviour.
    """
    where, wp = account_clause(account_id)
    w = f" WHERE {where}" if where else ""
    totals = conn.execute(
        "SELECT COUNT(*) as total_submissions, COALESCE(SUM(views),0) as total_views, "
        "COALESCE(SUM(favorites_count),0) as total_favorites, COALESCE(SUM(comments_count),0) as total_comments "
        "FROM fa_submissions" + w,
        wp,
    ).fetchone()
    # No offsets applied -- unlike IB, FA does not track deleted submissions.
    totals = dict(totals)

    top_viewed = conn.execute(
        "SELECT submission_id, title, views, thumbnail_url as thumb_url FROM fa_submissions" + w + " ORDER BY views DESC LIMIT 5",
        wp,
    ).fetchall()

    top_faved = conn.execute(
        "SELECT submission_id, title, favorites_count, thumbnail_url as thumb_url FROM fa_submissions" + w + " ORDER BY favorites_count DESC LIMIT 5",
        wp,
    ).fetchall()

    # Fastest-growing: same LEFT JOIN subquery pattern as IB.
    # Finds the nearest snapshot >= 24h old per submission, computes the
    # delta between current stats and that snapshot. Only the outer `s` needs
    # account scoping -- submission_ids are unique to their account, so the
    # snapshot subquery is implicitly account-correct.
    sw, sp = account_clause(account_id, "s")
    fastest_growing = conn.execute(
        """SELECT s.submission_id, s.title, s.thumbnail_url as thumb_url,
                  COALESCE(s.views - oldest.views, 0) as views_gained,
                  COALESCE(s.favorites_count - oldest.favorites_count, 0) as faves_gained
           FROM fa_submissions s
           LEFT JOIN (
               SELECT s1.submission_id, s1.views, s1.favorites_count
               FROM fa_snapshots s1
               INNER JOIN (
                   SELECT submission_id, MAX(polled_at) as max_polled
                   FROM fa_snapshots
                   WHERE polled_at <= datetime('now', '-24 hours')
                   GROUP BY submission_id
               ) s2 ON s1.submission_id = s2.submission_id AND s1.polled_at = s2.max_polled
           ) oldest ON s.submission_id = oldest.submission_id
           WHERE """ + (sw + " AND " if sw else "") + """COALESCE(s.views - oldest.views, 0) > 0
           ORDER BY views_gained DESC LIMIT 5""",
        sp,
    ).fetchall()

    # No recent_faves here -- FA does not expose individual fave users.
    recent_comments = get_fa_recent_comments(conn, limit=10, account_id=account_id)

    return {
        "total_submissions": totals["total_submissions"],
        "total_views": totals["total_views"],
        "total_favorites": totals["total_favorites"],
        "total_comments": totals["total_comments"],
        "top_viewed": [dict(r) for r in top_viewed],
        "top_faved": [dict(r) for r in top_faved],
        "fastest_growing": [dict(r) for r in fastest_growing],
        "recent_comments": recent_comments,
    }


def _calc_growth_rate(current: int, past: int | None, hours: int) -> float | None:
    """Daily growth rate formula: (current - past) / (hours / 24).
    Same helper as in queries.py -- duplicated here to keep each module
    self-contained without cross-module imports."""
    if past is None:
        return None
    delta = current - past
    days = hours / 24.0
    return round(delta / days, 2) if days > 0 else None


def get_fa_growth_rates(conn: sqlite3.Connection) -> dict:
    """Aggregate FA growth rates for 24h, 7d, 30d.

    Same approach as queries.get_growth_rates but without stat offsets --
    FA does not track deleted/private submission stats separately.
    """
    totals = conn.execute(
        "SELECT COALESCE(SUM(views),0) as views, COALESCE(SUM(favorites_count),0) as faves, "
        "COALESCE(SUM(comments_count),0) as comments FROM fa_submissions"
    ).fetchone()
    # No offsets applied -- unlike IB, FA totals are used as-is.
    current_views = totals["views"]
    current_faves = totals["faves"]
    current_comments = totals["comments"]

    rates = {}
    for label, hours in [("24h", 24), ("7d", 168), ("30d", 720)]:
        # Find the nearest past snapshot timestamp and sum across all
        # FA submissions at that timestamp. Same subquery pattern as IB.
        row = conn.execute(
            """SELECT SUM(views) as views, SUM(favorites_count) as faves, SUM(comments_count) as comments
               FROM fa_snapshots WHERE polled_at = (
                   SELECT polled_at FROM fa_snapshots
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


def get_fa_submission_growth_rates(conn: sqlite3.Connection, submission_id: int) -> dict:
    """Per-submission FA growth rates for 24h, 7d, 30d.
    Same approach as queries.get_submission_growth_rates on fa_snapshots."""
    sub = conn.execute(
        "SELECT views, favorites_count, comments_count FROM fa_submissions WHERE submission_id = ?",
        (submission_id,),
    ).fetchone()
    if not sub:
        return {}

    rates = {}
    for label, hours in [("24h", 24), ("7d", 168), ("30d", 720)]:
        row = conn.execute(
            """SELECT views, favorites_count as faves, comments_count as comments
               FROM fa_snapshots WHERE submission_id = ? AND polled_at <= datetime('now', ? || ' hours')
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


def get_fa_submission_deltas(conn: sqlite3.Connection) -> dict[int, dict]:
    """24h deltas for each FA submission.
    Same LEFT JOIN subquery pattern as queries.get_submission_deltas,
    operating on fa_submissions and fa_snapshots tables."""
    rows = conn.execute(
        """SELECT s.submission_id,
                  COALESCE(s.views - old.views, 0) as views_delta,
                  COALESCE(s.favorites_count - old.favorites_count, 0) as faves_delta,
                  COALESCE(s.comments_count - old.comments_count, 0) as comments_delta
           FROM fa_submissions s
           LEFT JOIN (
               SELECT s1.submission_id, s1.views, s1.favorites_count, s1.comments_count
               FROM fa_snapshots s1
               INNER JOIN (
                   SELECT submission_id, MAX(polled_at) as max_polled
                   FROM fa_snapshots
                   WHERE polled_at <= datetime('now', '-24 hours')
                   GROUP BY submission_id
               ) s2 ON s1.submission_id = s2.submission_id AND s1.polled_at = s2.max_polled
           ) old ON s.submission_id = old.submission_id"""
    ).fetchall()
    return {r["submission_id"]: dict(r) for r in rows}


# ── FA Watcher Queries ────────────────────────────────────────────

def upsert_fa_watcher(conn: sqlite3.Connection, account_id: int, username: str) -> bool:
    """Insert an FA watcher if not already tracked, or update last_seen_at.

    New watchers start with confirmed=0 (pending). On the next poll cycle,
    if they're still present, confirm_pending_watchers() promotes them to
    confirmed=1. This prevents spam bots (which get banned quickly) from
    ever triggering a notification.

    Returns True if the watcher is brand new (first insert).
    """
    try:
        conn.execute(
            "INSERT INTO fa_watchers (account_id, username, first_seen_at, last_seen_at, confirmed, notified) "
            "VALUES (?, ?, datetime('now'), datetime('now'), 0, 0)",
            (account_id, username),
        )
        return True
    except sqlite3.IntegrityError:
        # Already exists -- update last_seen_at to track continued presence
        conn.execute(
            "UPDATE fa_watchers SET last_seen_at = datetime('now') WHERE account_id = ? AND username = ?",
            (account_id, username),
        )
        return False


def confirm_pending_watchers(conn: sqlite3.Connection, account_id: int) -> list[str]:
    """Promote pending watchers (confirmed=0) that are still present (last_seen_at
    updated this cycle) to confirmed=1. Returns list of newly confirmed usernames.

    This is the core of the confirmation delay: watchers must survive at least
    2 consecutive poll cycles to be confirmed. Bots that get banned between
    polls are never promoted and never trigger notifications.
    """
    # A pending watcher whose last_seen_at was updated (i.e. still in the
    # FAExport list) gets promoted. We check that last_seen_at > first_seen_at
    # (meaning it was refreshed at least once after initial insert).
    rows = conn.execute(
        "SELECT username FROM fa_watchers WHERE account_id = ? AND confirmed = 0 AND last_seen_at > first_seen_at",
        (account_id,),
    ).fetchall()
    confirmed_names = [r["username"] for r in rows]
    if confirmed_names:
        conn.execute(
            "UPDATE fa_watchers SET confirmed = 1 WHERE account_id = ? AND confirmed = 0 AND last_seen_at > first_seen_at",
            (account_id,),
        )
    return confirmed_names


def mark_watchers_spam(conn: sqlite3.Connection, account_id: int, usernames: list[str]) -> None:
    """Flag watchers as spam (is_spam=1) for this account. They remain in the DB
    but are excluded from notifications."""
    if not usernames:
        return
    placeholders = ",".join("?" for _ in usernames)
    conn.execute(
        f"UPDATE fa_watchers SET is_spam = 1 WHERE account_id = ? AND username IN ({placeholders})",
        [account_id, *usernames],
    )


def get_unnotified_confirmed_watchers(conn: sqlite3.Connection, account_id: int) -> list[str]:
    """Get confirmed, non-spam watchers for this account that haven't been notified yet."""
    rows = conn.execute(
        "SELECT username FROM fa_watchers WHERE account_id = ? AND confirmed = 1 AND notified = 0 AND is_spam = 0",
        (account_id,),
    ).fetchall()
    return [r["username"] for r in rows]


def mark_watchers_notified(conn: sqlite3.Connection, account_id: int, usernames: list[str]) -> None:
    """Mark this account's watchers as notified so we don't re-notify."""
    if not usernames:
        return
    placeholders = ",".join("?" for _ in usernames)
    conn.execute(
        f"UPDATE fa_watchers SET notified = 1 WHERE account_id = ? AND username IN ({placeholders})",
        [account_id, *usernames],
    )


def remove_stale_fa_watchers(conn: sqlite3.Connection, account_id: int, current_usernames: list[str]) -> int:
    """Remove this account's FA watchers no longer on the live watcher list.

    Accounts that get banned, deleted, or unwatch disappear from FAExport.
    This prunes the DB to match reality, scoped to one account. Returns rows deleted.
    """
    if not current_usernames:
        return 0
    placeholders = ",".join("?" for _ in current_usernames)
    cur = conn.execute(
        f"DELETE FROM fa_watchers WHERE account_id = ? AND username NOT IN ({placeholders})",
        [account_id, *current_usernames],
    )
    return cur.rowcount


def get_fa_watchers_count(conn: sqlite3.Connection) -> int:
    """Total number of tracked FA watchers (confirmed only)."""
    row = conn.execute("SELECT COUNT(*) as c FROM fa_watchers WHERE confirmed = 1").fetchone()
    return row["c"] if row else 0


def get_fa_recent_watchers(conn: sqlite3.Connection, limit: int = 20) -> list[dict]:
    """Most recent FA watchers, newest first."""
    rows = conn.execute(
        "SELECT * FROM fa_watchers ORDER BY first_seen_at DESC LIMIT ?",
        (limit,),
    ).fetchall()
    return [dict(r) for r in rows]


# ── FA Profile Stats ─────────────────────────────────────────

def insert_fa_profile_stats(conn: sqlite3.Connection, account_id: int, pageviews: int, polled_at: str | None = None) -> None:
    """Record a profile pageviews snapshot for this account. One row per poll cycle."""
    ts = polled_at or datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    conn.execute(
        "INSERT INTO fa_profile_stats (account_id, polled_at, pageviews) VALUES (?, ?, ?)",
        (account_id, ts, pageviews),
    )


def get_fa_latest_profile_stats(conn: sqlite3.Connection) -> dict | None:
    """Get the most recent profile stats row."""
    row = conn.execute(
        "SELECT * FROM fa_profile_stats ORDER BY polled_at DESC LIMIT 1"
    ).fetchone()
    return dict(row) if row else None


def get_fa_profile_stats_history(conn: sqlite3.Connection, start: str | None = None, end: str | None = None) -> list[dict]:
    """Time-series of profile pageviews with optional date range."""
    sql = "SELECT polled_at, pageviews FROM fa_profile_stats"
    params: list[Any] = []
    conditions = []
    if start:
        conditions.append("polled_at >= ?")
        params.append(start)
    if end:
        conditions.append("polled_at <= ?")
        params.append(end)
    if conditions:
        sql += " WHERE " + " AND ".join(conditions)
    sql += " ORDER BY polled_at ASC"
    return [dict(r) for r in conn.execute(sql, params).fetchall()]
