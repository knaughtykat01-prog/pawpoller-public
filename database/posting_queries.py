"""CRUD operations for the posting module tables.

Three tables:
    publications    Registry of what has been posted where. One row per
                    (story_name, chapter_index, platform) combination.
                    Stores the external submission ID so updates can target it.
    posting_queue   Pending uploads and updates with scheduling support.
                    Items carry a 'requires' field (desktop/server/any) so the
                    scheduler only processes items valid for the current runtime.
    posting_log     Immutable audit trail. Every post, edit, or failure is
                    recorded here for debugging and history display.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timezone

from database import platform_metrics

logger = logging.getLogger(__name__)


# ── Publications ──────────────────────────────────────────────

def upsert_publication(
    conn: sqlite3.Connection,
    story_name: str,
    chapter_index: int,
    platform: str,
    *,
    account_id: int | None = None,
    content_type: str = "story",
    external_id: str = "",
    external_url: str = "",
    title_used: str = "",
    description_used: str = "",
    tags_used: list[str] | None = None,
    rating_used: str = "",
    format_file: str = "",
    file_hash: str = "",
    word_count: int = 0,
    status: str = "posted",
) -> int:
    """Insert or update a publication record. Returns pub_id.

    account_id selects which account the story was posted as; None resolves to
    the platform's default account, so single-account callers are unaffected.
    The publications UNIQUE key now includes account_id, so the same chapter can
    be published to two accounts on the same platform.
    """
    if account_id is None:
        from database import accounts as _accts
        account_id = _accts.get_default_account_id(conn, platform, create=True)
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    tags_json = json.dumps(tags_used or [])

    # Check if exists (scoped to the account + content_type).
    row = conn.execute(
        "SELECT pub_id, update_count FROM publications "
        "WHERE content_type = ? AND story_name = ? AND chapter_index = ? "
        "AND platform = ? AND account_id = ?",
        (content_type, story_name, chapter_index, platform, account_id),
    ).fetchone()

    if row:
        pub_id = row["pub_id"]
        update_count = row["update_count"] + 1
        conn.execute(
            """UPDATE publications SET
                external_id = ?, external_url = ?, title_used = ?,
                description_used = ?, tags_used = ?, rating_used = ?,
                format_file = ?, file_hash = ?, word_count = ?, status = ?,
                last_updated_at = ?, update_count = ?
            WHERE pub_id = ?""",
            (external_id, external_url, title_used, description_used,
             tags_json, rating_used, format_file, file_hash, word_count, status,
             now, update_count, pub_id),
        )
    else:
        cursor = conn.execute(
            """INSERT INTO publications
                (content_type, story_name, chapter_index, platform, account_id,
                 external_id, external_url,
                 title_used, description_used, tags_used, rating_used,
                 format_file, file_hash, word_count, status, first_posted_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (content_type, story_name, chapter_index, platform, account_id,
             external_id, external_url,
             title_used, description_used, tags_json, rating_used,
             format_file, file_hash, word_count, status, now),
        )
        pub_id = cursor.lastrowid

    conn.commit()
    return pub_id


def get_publications(
    conn: sqlite3.Connection,
    story_name: str | None = None,
    platform: str | None = None,
    status: str | None = None,
    content_type: str | None = "story",
) -> list[dict]:
    """Get publications with optional filters.

    content_type defaults to "story" so the Stories views never see artwork
    rows; pass "artwork" for the Artwork hub or None for everything.
    """
    query = "SELECT * FROM publications WHERE 1=1"
    params: list = []
    if content_type is not None:
        query += " AND content_type = ?"
        params.append(content_type)
    if story_name:
        query += " AND story_name = ?"
        params.append(story_name)
    if platform:
        query += " AND platform = ?"
        params.append(platform)
    if status:
        query += " AND status = ?"
        params.append(status)
    query += " ORDER BY story_name, chapter_index, platform"
    return [dict(r) for r in conn.execute(query, params).fetchall()]


def get_publication(conn: sqlite3.Connection, pub_id: int) -> dict | None:
    """Get a single publication by ID."""
    row = conn.execute("SELECT * FROM publications WHERE pub_id = ?", (pub_id,)).fetchone()
    return dict(row) if row else None


def get_publications_with_stats(
    conn: sqlite3.Connection,
    story_name: str | None = None,
    content_type: str | None = "story",
) -> list[dict]:
    """Get publications enriched with live stats from the polling submission tables.

    Joins each publication's external_id with the platform-specific submission table
    to pull in current views, faves, comments counts. Because pollers auto-discover
    the whole gallery, artwork rows enrich from the same submission tables — pass
    content_type="artwork" for the Artwork hub.
    """
    pubs = get_publications(conn, story_name=story_name, status="posted",
                            content_type=content_type)

    # Platform → table/column knowledge lives in ONE place now
    # (database/platform_metrics.py). This function used to carry its own copy
    # covering 7 of 19 platforms, and its AO3/SqW entry asked for `hits`/`kudos`
    # — columns that don't exist. The resulting `no such column` was swallowed
    # by a bare `except: continue`, so every AO3 + SquidgeWorld publication
    # reported no stats at all, and e621/Twitter/DA/Itaku/Bluesky/Instagram/
    # Mastodon/Threads/Tumblr/Pixiv/FN/Furbooru were never looked up. Both are
    # fixed by deferring to the registry; read_stats logs loudly rather than
    # swallowing a mismatch.
    #
    # Perf guardrail (2.165.0): the old code ran ONE stat query per publication —
    # O(pubs) queries, which the Library's /api/works list paid on every load.
    # Batch it: group external_ids by platform and fetch each platform's stats in
    # one query (chunked under SQLite's variable cap), then assign in Python.
    ids_by_plat: dict[str, set] = {}
    for pub in pubs:
        plat = pub["platform"]
        ext = pub["external_id"]
        if ext and platform_metrics.get(plat):
            ids_by_plat.setdefault(plat, set()).add(ext)

    stats_map: dict[tuple, dict] = {}     # (platform, str(external_id)) -> stats
    for plat, ids in ids_by_plat.items():
        for ext_id, stats in platform_metrics.read_stats(conn, plat, ids).items():
            stats_map[(plat, ext_id)] = stats

    enriched = []
    for pub in pubs:
        pub_dict = dict(pub) if not isinstance(pub, dict) else pub
        pub_dict["stats"] = stats_map.get(
            (pub_dict["platform"], str(pub_dict["external_id"] or "")))
        enriched.append(pub_dict)

    return enriched


def get_publication_by_story(
    conn: sqlite3.Connection,
    story_name: str,
    chapter_index: int,
    platform: str,
    account_id: int | None = None,
    content_type: str = "story",
) -> dict | None:
    """Get a publication by its (content_type, story, chapter, platform[, account]) key.

    account_id None resolves to the platform's default account so existing
    single-account callers keep getting the default account's row. content_type
    defaults to "story"; the Artwork hub passes "artwork".
    """
    if account_id is None:
        from database import accounts as _accts
        account_id = _accts.get_default_account_id(conn, platform, create=True)
    row = conn.execute(
        "SELECT * FROM publications WHERE content_type = ? AND story_name = ? "
        "AND chapter_index = ? AND platform = ? AND account_id = ?",
        (content_type, story_name, chapter_index, platform, account_id),
    ).fetchone()
    return dict(row) if row else None


# ── Posting Queue ─────────────────────────────────────────────

# Only these can ever be cleared. `pending` and `processing` are live work —
# a clear that could reach them would delete a scheduled post, or orphan a row
# the scheduler is mid-way through. The set is a frozenset rather than a
# convention so a caller cannot widen it by passing a string.
CLEARABLE_STATUSES = frozenset({"failed", "cancelled", "completed"})


def count_queue_by_status(conn: sqlite3.Connection) -> dict:
    """Clearable row counts, per status and per platform.

    Feeds the Queue page's "Clear finished rows" button so the confirm names a
    real number instead of asking the operator to trust one.
    """
    out: dict = {"by_status": {}, "by_platform": {}, "total": 0}
    for status, n in conn.execute(
            "SELECT status, COUNT(*) FROM posting_queue WHERE status IN "
            "('failed', 'cancelled', 'completed') GROUP BY status"):
        out["by_status"][status] = n
        out["total"] += n
    for platform, n in conn.execute(
            "SELECT platform, COUNT(*) FROM posting_queue WHERE status = 'failed' "
            "GROUP BY platform ORDER BY COUNT(*) DESC"):
        out["by_platform"][platform] = n
    return out


def clear_queue_rows(conn: sqlite3.Connection, statuses) -> int:
    """Delete finished queue rows. Returns how many went.

    Deliberately a DELETE, not another status flag: these rows are already
    terminal, and the thing being fixed is that there are thousands of them.

    Refuses any status outside :data:`CLEARABLE_STATUSES`, so no caller — and
    no future endpoint — can reach `pending` or `processing` work. The guard is
    a hard failure rather than a filter: silently dropping an unexpected status
    would let "clear everything" look like it worked while leaving live rows
    behind, which is the more dangerous of the two mistakes.

    Written for the ~4,400 rows one dead DeviantArt token accumulated through
    the retry bug fixed in 3.21.0 (919 da / 3,208 ao3 / 267 ik). Those rows are
    inert, but they make the Queue page load every one of them and bury real
    work. The fix stopped them being created; this clears the ones already
    there, on the operator's button rather than a migration.
    """
    statuses = [str(s) for s in statuses]
    if not statuses:
        return 0
    bad = sorted(set(statuses) - CLEARABLE_STATUSES)
    if bad:
        raise ValueError(
            f"refusing to clear queue rows with status {bad} — only "
            f"{sorted(CLEARABLE_STATUSES)} are finished work")
    placeholders = ",".join("?" for _ in statuses)
    cursor = conn.execute(
        f"DELETE FROM posting_queue WHERE status IN ({placeholders})", statuses)
    conn.commit()
    return cursor.rowcount


def count_retry_rows(
    conn: sqlite3.Connection,
    story_name: str,
    chapter_index: int,
    platform: str,
    action: str,
    *,
    content_type: str = "story",
    account_id: int | None = None,
) -> int:
    """How many retries this target has already had in the current campaign.

    Retry rows are the ones with ``priority = -1``; a campaign restarts at the
    newest hand-queued row (``priority >= 0``) for the same target, so an old
    run of failures can never permanently bar a fresh attempt.

    This is what makes the three-attempt ceiling real. ``_schedule_retry`` was
    handed a literal ``0`` by every one of its callers, so ``attempt >=
    max_attempts`` was never true and each failure queued yet another row. One
    expired DeviantArt token had produced 919 queue rows this way, re-hitting
    DA's token endpoint every five seconds for three days; AO3 had 3,208 rows
    and Itaku 267 from the same shape.
    """
    where = ("content_type = ? AND story_name = ? AND chapter_index = ? "
             "AND platform = ? AND action = ?")
    params: list = [content_type, story_name, chapter_index, platform, action]
    if account_id is not None:
        where += " AND account_id = ?"
        params.append(account_id)

    row = conn.execute(
        f"SELECT MAX(created_at) FROM posting_queue WHERE {where} AND priority >= 0",
        params,
    ).fetchone()
    since = (row[0] if row and row[0] else "") or ""

    row = conn.execute(
        f"SELECT COUNT(*) FROM posting_queue WHERE {where} AND priority < 0 "
        "AND created_at > ?",
        params + [since],
    ).fetchone()
    return int(row[0]) if row else 0


def add_to_queue(
    conn: sqlite3.Connection,
    story_name: str,
    chapter_index: int,
    platform: str,
    action: str = "post",
    *,
    account_id: int | None = None,
    content_type: str = "story",
    scheduled_at: str | None = None,
    title_override: str | None = None,
    description_override: str | None = None,
    tags_override: str | None = None,
    rating_override: str | None = None,
    file_path_override: str | None = None,
    priority: int = 0,
    requires: str = "any",
    drip_group: str | None = None,
) -> int:
    """Add an item to the posting queue. Returns queue_id.

    Args:
        account_id: Which account to post as; None → the platform's default.
            The scheduler posts queued items as this account (important for the
            desktop FA auto-queue so the right account is used).
        requires: Runtime mode needed — 'any', 'desktop', or 'server'.
            Desktop-only platforms (FA) should be queued with 'desktop' so the
            server scheduler skips them and they're picked up when the desktop app opens.
        drip_group: Campaign id shared by all rows of one "drip schedule"
            (gap G1) so the whole drip can be cancelled as a unit. None for
            ordinary one-off schedules.
    """
    if account_id is None:
        from database import accounts as _accts
        account_id = _accts.get_default_account_id(conn, platform, create=True)
    cursor = conn.execute(
        """INSERT INTO posting_queue
            (content_type, story_name, chapter_index, platform, account_id, action,
             scheduled_at, title_override, description_override, tags_override,
             rating_override, file_path_override, priority, requires, drip_group)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (content_type, story_name, chapter_index, platform, account_id, action,
         scheduled_at, title_override, description_override, tags_override,
         rating_override, file_path_override, priority, requires, drip_group),
    )
    conn.commit()
    return cursor.lastrowid


def get_pending_queue(
    conn: sqlite3.Connection,
    limit: int = 20,
    runtime_mode: str | None = None,
) -> list[dict]:
    """Get pending queue items ordered by priority then creation time.

    When ``runtime_mode`` is provided, only items whose ``requires`` field is
    ``'any'`` or matches the mode are returned. This stops a head-of-line block
    where stale ``requires='desktop'`` rows (e.g. from a removed FA auto-queue
    path) sit at the top of the FIFO and starve newer compatible items past
    the LIMIT — the bug item 8 hit when items 1–7 were April-dated zombies.
    """
    if runtime_mode is None:
        rows = conn.execute(
            """SELECT * FROM posting_queue
            WHERE status = 'pending'
              AND (scheduled_at IS NULL OR scheduled_at <= datetime('now'))
            ORDER BY priority DESC, created_at ASC
            LIMIT ?""",
            (limit,),
        ).fetchall()
    else:
        rows = conn.execute(
            """SELECT * FROM posting_queue
            WHERE status = 'pending'
              AND (scheduled_at IS NULL OR scheduled_at <= datetime('now'))
              AND requires IN ('any', ?)
            ORDER BY priority DESC, created_at ASC
            LIMIT ?""",
            (runtime_mode, limit),
        ).fetchall()
    return [dict(r) for r in rows]


def get_queue(
    conn: sqlite3.Connection,
    include_completed: bool = False,
    story_name: str | None = None,
    content_type: str | None = "story",
) -> list[dict]:
    """Get queue items, optionally filtered by story.

    The story_name filter is used by the story detail page to render only
    that story's pending items as a callout card. content_type defaults to
    "story" so the Stories queue view never shows artwork; the Artwork hub
    passes "artwork".
    """
    params: list = []
    if include_completed:
        query = "SELECT * FROM posting_queue"
        order = " ORDER BY created_at DESC"
    else:
        query = "SELECT * FROM posting_queue WHERE status IN ('pending', 'processing')"
        order = " ORDER BY priority DESC, created_at ASC"

    if story_name:
        if "WHERE" in query:
            query += " AND story_name = ?"
        else:
            query += " WHERE story_name = ?"
        params.append(story_name)

    if content_type is not None:
        if "WHERE" in query:
            query += " AND content_type = ?"
        else:
            query += " WHERE content_type = ?"
        params.append(content_type)

    rows = conn.execute(query + order, params).fetchall()
    return [dict(r) for r in rows]


def claim_queue_item(
    conn: sqlite3.Connection,
    queue_id: int,
    claimed_by: str | None = None,
) -> bool:
    """Atomically take ownership of a pending queue item. True if we got it.

    This is deliberately NOT ``update_queue_status(..., "processing")``. That
    call guards on ``status != 'cancelled'``, which makes a user-issued cancel
    stick but does nothing to stop a SECOND scheduler moving the same row to
    'processing' and posting it again. The guard here is ``status = 'pending'``,
    so exactly one caller can transition a row out of pending — SQLite
    serialises the write, and the loser sees rowcount 0.

    It matters because the desktop and the server both start the posting
    scheduler (``main.py``, ``server.py``). Today the two run against separate
    databases so the race cannot happen; the moment the queue is shared it
    posts the same work twice to a live platform. Claiming correctly is a
    prerequisite for ever mirroring this table
    (``docs/specs/desktop_server_mirroring.md``).

    Losing the claim is normal, not an error: another instance got there first,
    or the item was cancelled between the SELECT and here.
    """
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    cursor = conn.execute(
        "UPDATE posting_queue SET status = 'processing', started_at = ?, "
        "attempts = attempts + 1, claimed_by = ? "
        "WHERE queue_id = ? AND status = 'pending'",
        (now, claimed_by, queue_id),
    )
    conn.commit()
    return cursor.rowcount > 0


def update_queue_status(
    conn: sqlite3.Connection,
    queue_id: int,
    status: str,
    *,
    error: str | None = None,
    pub_id: int | None = None,
) -> None:
    """Update a queue item's status.

    Refuses to overwrite a 'cancelled' row. The scheduler resets a row
    to 'pending' on failure for retry; without this guard, a user-issued
    cancel mid-flight gets clobbered by the scheduler's failure handler
    the moment the in-flight post errors out, and the next scheduler
    tick picks the row back up. The guard makes cancel actually stick.
    """
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    if status == "processing":
        conn.execute(
            "UPDATE posting_queue SET status = ?, started_at = ?, attempts = attempts + 1 "
            "WHERE queue_id = ? AND status != 'cancelled'",
            (status, now, queue_id),
        )
    elif status in ("completed", "failed"):
        conn.execute(
            "UPDATE posting_queue SET status = ?, completed_at = ?, last_error = ?, pub_id = ? "
            "WHERE queue_id = ? AND status != 'cancelled'",
            (status, now, error, pub_id, queue_id),
        )
    else:
        conn.execute(
            "UPDATE posting_queue SET status = ? WHERE queue_id = ? AND status != 'cancelled'",
            (status, queue_id),
        )
    conn.commit()


def cancel_queue_item(conn: sqlite3.Connection, queue_id: int) -> bool:
    """Cancel a queue item if it's in a cancellable state.

    Cancellable: pending, failed (rare manual cleanup after a giving-up
    event). 'processing' rows are mid-flight in the scheduler — cancel
    marks them, and the scheduler treats 'cancelled' as a terminal state
    when it completes the in-flight work, so the next retry won't fire.
    ('retrying' was never actually written — retries enqueue a fresh
    'pending' row — so it's dropped from the cancellable set.)
    """
    cursor = conn.execute(
        "UPDATE posting_queue SET status = 'cancelled' "
        "WHERE queue_id = ? AND status IN ('pending', 'processing', 'failed')",
        (queue_id,),
    )
    conn.commit()
    return cursor.rowcount > 0


def cancel_all_for(conn: sqlite3.Connection, *, platform: str | None = None,
                   story_name: str | None = None,
                   chapter_index: int | None = None,
                   content_type: str | None = None,
                   drip_group: str | None = None) -> int:
    """Bulk-cancel queue items matching the filter. Used by the editor's
    'cancel all retries for X' affordance and the diagnostics cleanup
    flow when a poster bug spams the queue.

    Returns the number of rows cancelled. Filters compose with AND
    semantics; all filters None means cancel-everything-non-terminal
    which is rarely what callers want — explicit non-None args strongly
    recommended.
    """
    sql = (
        "UPDATE posting_queue SET status = 'cancelled' "
        "WHERE status IN ('pending', 'processing', 'failed')"
    )
    params: list = []
    if platform is not None:
        sql += " AND platform = ?"
        params.append(platform)
    if story_name is not None:
        sql += " AND story_name = ?"
        params.append(story_name)
    if chapter_index is not None:
        sql += " AND chapter_index = ?"
        params.append(chapter_index)
    if content_type is not None:
        sql += " AND content_type = ?"
        params.append(content_type)
    if drip_group is not None:
        sql += " AND drip_group = ?"
        params.append(drip_group)
    cursor = conn.execute(sql, params)
    conn.commit()
    return cursor.rowcount


def reschedule_queue_item(
    conn: sqlite3.Connection, queue_id: int, scheduled_at: str
) -> bool:
    """Move a still-pending queue item to a new scheduled time.

    Only touches rows that are still 'pending' — a row already
    processing/completed/failed/cancelled can't be moved (the work is
    done or in flight). ``scheduled_at`` is a UTC 'YYYY-MM-DD HH:MM:SS'
    string, the same shape the scheduler compares against datetime('now').
    Returns True if a row was moved.
    """
    cursor = conn.execute(
        "UPDATE posting_queue SET scheduled_at = ? "
        "WHERE queue_id = ? AND status = 'pending'",
        (scheduled_at, queue_id),
    )
    conn.commit()
    return cursor.rowcount > 0


def get_scheduled_items(conn: sqlite3.Connection) -> list[dict]:
    """All pending queue items carrying a scheduled_at, across every
    content_type, soonest first. Backs the global "what's going out and
    when" agenda. Items with no scheduled_at (they fire on the next tick)
    are excluded — they're queued, not scheduled.
    """
    rows = conn.execute(
        "SELECT * FROM posting_queue "
        "WHERE status = 'pending' AND scheduled_at IS NOT NULL "
        "ORDER BY scheduled_at ASC, priority DESC, created_at ASC"
    ).fetchall()
    return [dict(r) for r in rows]


def delete_publication(
    conn: sqlite3.Connection,
    story_name: str,
    chapter_index: int,
    platform: str,
    content_type: str = "story",
) -> bool:
    """Remove the publications row for (story, chapter, platform).

    Used by the "forget publication" affordance in the publish-check
    panel when the user has manually deleted the upstream submission
    and wants PawPoller's local memory cleared so the cell reverts to
    'ready' (next post is a fresh create, not an edit).

    Returns True if a row was deleted, False if no matching row existed.

    Two tables carry a ``pub_id`` foreign key back to ``publications`` —
    ``posting_queue`` and the immutable ``posting_log`` audit trail. With
    ``PRAGMA foreign_keys = ON`` a bare ``DELETE FROM publications`` raises
    ``FOREIGN KEY constraint failed`` the moment the row has ever been posted
    (a ``posting_log`` row references it). So we unlink the children first —
    both ``pub_id`` columns are nullable, so we NULL them rather than delete:
    the queue item keeps its story/chapter/platform identity and the audit
    log stays intact, they just lose the back-reference to the forgotten row.
    """
    rows = conn.execute(
        "SELECT pub_id FROM publications "
        "WHERE content_type = ? AND story_name = ? AND chapter_index = ? AND platform = ?",
        (content_type, story_name, chapter_index, platform),
    ).fetchall()
    if not rows:
        return False
    pub_ids = [r[0] for r in rows]
    placeholders = ",".join("?" * len(pub_ids))
    conn.execute(
        f"UPDATE posting_queue SET pub_id = NULL WHERE pub_id IN ({placeholders})",
        pub_ids,
    )
    conn.execute(
        f"UPDATE posting_log SET pub_id = NULL WHERE pub_id IN ({placeholders})",
        pub_ids,
    )
    cursor = conn.execute(
        f"DELETE FROM publications WHERE pub_id IN ({placeholders})",
        pub_ids,
    )
    conn.commit()
    return cursor.rowcount > 0


def update_publication_url(
    conn: sqlite3.Connection,
    story_name: str,
    chapter_index: int,
    platform: str,
    content_type: str = "story",
    *,
    external_url: str,
    external_id: str,
) -> bool:
    """Overwrite the URL + external ID of an existing publications row.

    Used by the "set URL manually" affordance when PawPoller's stored
    URL is wrong or empty but the upstream submission exists — letting
    the user paste the live URL and have edit/drift work correctly
    against it.

    Returns True if a row was updated, False if no matching row existed.
    """
    cursor = conn.execute(
        "UPDATE publications "
        "SET external_url = ?, external_id = ? "
        "WHERE content_type = ? AND story_name = ? AND chapter_index = ? AND platform = ?",
        (external_url, external_id, content_type, story_name, chapter_index, platform),
    )
    conn.commit()
    return cursor.rowcount > 0


# ── Posting Log ───────────────────────────────────────────────

def log_posting_action(
    conn: sqlite3.Connection,
    platform: str,
    story_name: str,
    chapter_index: int,
    action: str,
    status: str,
    *,
    account_id: int = 0,
    content_type: str = "story",
    pub_id: int | None = None,
    queue_id: int | None = None,
    external_id: str | None = None,
    external_url: str | None = None,
    error_message: str | None = None,
    duration_seconds: float | None = None,
) -> int:
    """Append an entry to the posting log. Returns log_id."""
    cursor = conn.execute(
        """INSERT INTO posting_log
            (pub_id, queue_id, platform, story_name, chapter_index, account_id, content_type,
             action, status, external_id, external_url, error_message, duration_seconds)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (pub_id, queue_id, platform, story_name, chapter_index, account_id, content_type,
         action, status, external_id, external_url, error_message, duration_seconds),
    )
    conn.commit()
    return cursor.lastrowid


def get_posting_log(
    conn: sqlite3.Connection,
    story_name: str | None = None,
    limit: int = 50,
    content_type: str | None = "story",
) -> list[dict]:
    """Get posting log entries, newest first.

    content_type defaults to "story" so the Stories log view never shows
    artwork; the Artwork hub passes "artwork", None returns everything.
    """
    query = "SELECT * FROM posting_log"
    params: list = []
    conds = []
    if content_type is not None:
        conds.append("content_type = ?")
        params.append(content_type)
    if story_name:
        conds.append("story_name = ?")
        params.append(story_name)
    if conds:
        query += " WHERE " + " AND ".join(conds)
    query += " ORDER BY created_at DESC LIMIT ?"
    params.append(limit)
    return [dict(r) for r in conn.execute(query, params).fetchall()]


# ── Publish-matrix helpers (3.28.0) ──────────────────────────────────────
#
# The publish matrix is a grid of chapter × platform. A publication is not:
# its UNIQUE key is (story, chapter, platform, account_id, content_type), so
# one work posted to one platform from two accounts is TWO rows competing for
# ONE cell. The matrix indexed them with a plain dict comprehension, which kept
# whichever row the query happened to return last — one account's publication
# silently vanished, and which one vanished depended on row order.
#
# Stories haven't hit it (each posts from one account) but artwork already has
# seven such pairs live. Both helpers below live here rather than in the route
# so the Artwork hub can reuse them when it grows the same grid.

#: Which publication outranks which when two share a cell. A live post beats a
#: dead one; an unknown status is treated as `failed` rather than as the winner,
#: because promoting something we can't classify is the more dangerous default.
PUBLICATION_STATUS_RANK = {
    "posted": 3,
    "draft": 2,
    "failed": 1,
    "deleted": 0,
}


def index_publications_by_cell(
    pubs: list[dict],
) -> tuple[dict[tuple[int, str], dict], dict[tuple[int, str], set]]:
    """Collapse per-account publications onto (chapter_index, platform) cells.

    Returns ``(best, accounts)``: the publication to display in each cell, and
    the full set of account ids that have one there. A caller that shows
    ``len(accounts[cell]) > 1`` tells the truth about the rows it isn't
    showing; one that ignores it is back to silently dropping them.

    Ranked by status first, then by ``first_posted_at`` so the most recent wins
    a tie. Deterministic either way — the point is that the answer can't depend
    on the order SQLite happened to return.
    """
    def _rank(p: dict) -> tuple:
        return (PUBLICATION_STATUS_RANK.get(p["status"], 1), p["first_posted_at"] or "")

    best: dict[tuple[int, str], dict] = {}
    accounts: dict[tuple[int, str], set] = {}
    for p in pubs:
        key = (p["chapter_index"], p["platform"])
        accounts.setdefault(key, set()).add(p["account_id"])
        current = best.get(key)
        if current is None or _rank(p) > _rank(current):
            best[key] = p
    return best, accounts


def count_active_jobs(conn: sqlite3.Connection, story_name: str) -> int:
    """Queue rows for this story that haven't finished yet.

    `pending` and `processing` are the two live states — everything else
    (`completed`, `failed`, `cancelled`) is done and will never change on its
    own. The publish matrix polls while this is non-zero and stops when it
    reaches zero, so an idle grid makes no requests at all. Counting the
    finished states here instead would leave a matrix polling forever after a
    single failure.
    """
    return conn.execute(
        "SELECT COUNT(*) FROM posting_queue WHERE story_name = ? "
        "AND status IN ('pending', 'processing')",
        (story_name,),
    ).fetchone()[0]
