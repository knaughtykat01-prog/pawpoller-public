"""Unified comment inbox (gap G3) — storage + the cross-platform union query.

Two tables, created idempotently from _run_migrations (the followers pattern):

- ``platform_comments`` — per-comment rows for the platforms whose pollers
  capture content via cheap official GETs (bsky / mast / e621 / da, Stage A1).
  IB and FA keep their existing dedicated tables (``comments`` /
  ``fa_comments``) — battle-tested, threaded — and the inbox UNIONs them in.
  ``meta`` holds platform-specific reply refs as JSON (bsky needs uri/cid of
  both the reply and the root post to thread a native reply).
- ``inbox_state`` — the handled/replied flag for ANY comment, keyed
  (platform, comment_id), so no legacy table needs altering.

The union query (get_inbox) is the whole product: newest-first comments across
every source, each row shaped identically for the frontend, with a constructed
permalink and the handled flag joined on.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone


def ensure_inbox_tables(conn: sqlite3.Connection) -> None:
    """Create the inbox tables if missing. Idempotent; called every startup."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS platform_comments (
            platform        TEXT NOT NULL,
            comment_id      TEXT NOT NULL,
            submission_id   TEXT NOT NULL,
            account_id      INTEGER,
            author          TEXT NOT NULL DEFAULT '',
            body            TEXT NOT NULL DEFAULT '',
            commented_at    TEXT,
            first_seen_at   TEXT NOT NULL DEFAULT (datetime('now')),
            permalink       TEXT NOT NULL DEFAULT '',
            submission_title TEXT NOT NULL DEFAULT '',
            meta            TEXT NOT NULL DEFAULT '{}',
            is_own          INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (platform, comment_id)
        )""")
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_platform_comments_seen
            ON platform_comments(first_seen_at)""")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS inbox_state (
            platform    TEXT NOT NULL,
            comment_id  TEXT NOT NULL,
            handled_at  TEXT,
            PRIMARY KEY (platform, comment_id)
        )""")
    conn.commit()


def upsert_platform_comment(
    conn: sqlite3.Connection,
    platform: str,
    comment_id: str,
    submission_id: str,
    *,
    author: str = "",
    body: str = "",
    commented_at: str | None = None,
    permalink: str = "",
    submission_title: str = "",
    account_id: int | None = None,
    meta: dict | None = None,
    is_own: bool = False,
) -> bool:
    """Insert a captured comment; ignore if already seen. Returns True if new.

    INSERT OR IGNORE keeps first_seen_at stable (the notification-order
    timestamp) — platforms don't expose edit history worth tracking here.

    *is_own* marks a comment written by the posting account (2.192.0). Stored,
    not skipped: dropping it would leave the captured count permanently below
    the platform's reported count, so the delta check would re-fetch that thread
    every cycle forever.
    """
    cur = conn.execute(
        """INSERT OR IGNORE INTO platform_comments
            (platform, comment_id, submission_id, account_id, author, body,
             commented_at, permalink, submission_title, meta, is_own)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (platform, str(comment_id), str(submission_id), account_id,
         author, body, commented_at, permalink, submission_title,
         json.dumps(meta or {}), 1 if is_own else 0),
    )
    conn.commit()
    return cur.rowcount > 0


def count_for_submission(conn: sqlite3.Connection, platform: str,
                         submission_id: str) -> int:
    """How many comments we've captured for one submission — the poller's
    delta check (fresh platform count > this → fetch the thread). Self-healing:
    a missed fetch simply retries next cycle."""
    row = conn.execute(
        "SELECT COUNT(*) FROM platform_comments WHERE platform = ? AND submission_id = ?",
        (platform, str(submission_id)),
    ).fetchone()
    return int(row[0]) if row else 0


def set_handled(conn: sqlite3.Connection, platform: str, comment_id: str,
                handled: bool) -> None:
    if handled:
        conn.execute(
            """INSERT INTO inbox_state (platform, comment_id, handled_at)
               VALUES (?, ?, ?)
               ON CONFLICT(platform, comment_id)
               DO UPDATE SET handled_at = excluded.handled_at""",
            (platform, str(comment_id),
             datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")),
        )
    else:
        conn.execute("DELETE FROM inbox_state WHERE platform = ? AND comment_id = ?",
                     (platform, str(comment_id)))
    conn.commit()


def get_inbox(conn: sqlite3.Connection, *, platform: str | None = None,
              unhandled_only: bool = False, limit: int = 200) -> list[dict]:
    """The unified inbox: IB + FA legacy tables ∪ platform_comments, newest
    first (by when OUR poller first saw each comment — the user's notification
    experience), each row shaped identically:
    {platform, comment_id, submission_id, submission_title, author, body,
     commented_at, first_seen_at, permalink, handled, can_reply, meta, is_own}

    ``is_own`` rows (written by the posting account) stay in the feed as thread
    context but are always reported handled, so they can never inflate the
    "N to answer" count.

    IB/FA permalinks are constructed (submission page + comment anchor); the
    A1 platforms store theirs at capture time. Missing tables (legacy DBs) are
    skipped rather than failing the whole inbox.
    """
    rows: list[dict] = []

    def _grab(sql: str, args: tuple = ()):
        try:
            rows.extend(dict(r) for r in conn.execute(sql, args).fetchall())
        except sqlite3.OperationalError:
            pass  # table missing on a legacy/partial DB — skip that source

    # Inkbunny — dedicated table, join for the title.
    _grab("""
        SELECT 'ib' AS platform, CAST(c.comment_id AS TEXT) AS comment_id,
               CAST(c.submission_id AS TEXT) AS submission_id,
               COALESCE(s.title, '') AS submission_title,
               c.username AS author, c.comment_text AS body,
               c.commented_at, c.first_seen_at,
               'https://inkbunny.net/s/' || c.submission_id
                   || '#commentid_' || c.comment_id AS permalink,
               '{}' AS meta, COALESCE(c.is_own, 0) AS is_own
          FROM comments c LEFT JOIN submissions s USING (submission_id)""")

    # FurAffinity — dedicated table; hide moderator-deleted comments.
    _grab("""
        SELECT 'fa' AS platform, c.comment_id,
               CAST(c.submission_id AS TEXT) AS submission_id,
               COALESCE(s.title, '') AS submission_title,
               c.username AS author, c.comment_text AS body,
               c.commented_at, c.first_seen_at,
               'https://www.furaffinity.net/view/' || c.submission_id
                   || '/#cid:' || c.comment_id AS permalink,
               '{}' AS meta, COALESCE(c.is_own, 0) AS is_own
          FROM fa_comments c LEFT JOIN fa_submissions s USING (submission_id)
         WHERE COALESCE(c.is_deleted, 0) = 0""")

    # Stage-A1 platforms — unified capture table.
    _grab("""
        SELECT platform, comment_id, submission_id, submission_title,
               author, body, commented_at, first_seen_at, permalink, meta,
               COALESCE(is_own, 0) AS is_own
          FROM platform_comments""")

    # Handled flags in one read.
    handled = set()
    try:
        handled = {(r[0], r[1]) for r in
                   conn.execute("SELECT platform, comment_id FROM inbox_state "
                                "WHERE handled_at IS NOT NULL").fetchall()}
    except sqlite3.OperationalError:
        pass

    # Native reply is wired for these (Stage B); everything else is
    # "Reply on site ↗" via the permalink.
    replyable = {"bsky", "mast", "e621"}

    out = []
    for r in rows:
        if platform and r["platform"] != platform:
            continue
        r["is_own"] = bool(r.get("is_own"))
        # Your own comment is never something to answer, so it is forced handled
        # (2.192.0). Deriving it here rather than writing inbox_state at capture
        # time is deliberate: it also fixes rows captured BEFORE 2.192.0, and
        # rows the old auto-handle branch could never reach because it only ran
        # inside `if is_new:` and so never retro-handled an existing row.
        r["handled"] = r["is_own"] or (r["platform"], str(r["comment_id"])) in handled
        if unhandled_only and r["handled"]:
            continue
        r["can_reply"] = r["platform"] in replyable
        try:
            r["meta"] = json.loads(r.get("meta") or "{}")
        except (TypeError, ValueError):
            r["meta"] = {}
        out.append(r)

    out.sort(key=lambda r: r.get("first_seen_at") or "", reverse=True)
    return out[:limit]
