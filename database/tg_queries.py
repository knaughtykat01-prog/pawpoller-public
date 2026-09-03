"""Queries for Telegram channel posts and their reaction counts.

Two entry points matter:

* ``record_submission`` — called when PawPoller *sends* a post. This is what
  makes the submission list exact: we are the only writer, so nothing can drift.
* ``apply_reaction_count`` — called when a ``message_reaction_count`` update
  arrives. Reactions are pushed and cannot be queried, so this is the only way
  the numbers ever change.

⚠ A post with no reaction update yet has ``reactions_at IS NULL``, which is NOT
the same as zero reactions. The UI must render the first as "not counted" and
only the second as 0 — see docs/specs/telegram_platform.md.
"""
from __future__ import annotations

import json
import logging
import sqlite3

logger = logging.getLogger(__name__)


def make_submission_id(chat_id, message_id) -> str:
    """A message id is unique only within its chat, and one install may post to
    several channels — so the key has to carry both."""
    return f"{chat_id}:{message_id}"


def record_submission(conn: sqlite3.Connection, *, account_id: int, chat_id,
                      message_id, title: str = "", posted_at: str = "",
                      link: str = "", content_type: str = "artwork") -> str:
    """Record a post PawPoller just sent. Idempotent on (chat_id, message_id).

    Deliberately does NOT touch the reaction columns: a re-record must not wipe
    counts that arrived between the first write and this one.
    """
    sid = make_submission_id(chat_id, message_id)
    conn.execute(
        """
        INSERT INTO tg_submissions
            (submission_id, account_id, chat_id, message_id, title, posted_at,
             link, content_type)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(submission_id) DO UPDATE SET
            title        = excluded.title,
            link         = excluded.link,
            content_type = excluded.content_type,
            updated_at   = datetime('now')
        """,
        (sid, account_id, str(chat_id), int(message_id), title, posted_at,
         link, content_type),
    )
    return sid


def apply_reaction_count(conn: sqlite3.Connection, *, chat_id, message_id,
                         reactions: list[dict]) -> bool:
    """Apply a pushed reaction-count update. Returns True if a row was updated.

    ``reactions`` is Telegram's ``ReactionCount[]``: ``[{"type": {...},
    "total_count": n}, …]``. It is an ABSOLUTE state, not a delta — the update
    carries the full current set, so removing a reaction arrives as a smaller
    list rather than a negative number.

    Returns False for a message we never sent. That is normal, not an error:
    someone may react to a post made by hand in the channel, and PawPoller only
    tracks its own.
    """
    sid = make_submission_id(chat_id, message_id)
    simple, total = [], 0
    for r in reactions or []:
        t = r.get("type") or {}
        emoji = t.get("emoji") or t.get("custom_emoji_id") or "?"
        count = int(r.get("total_count") or 0)
        total += count
        simple.append({"emoji": emoji, "count": count})

    cur = conn.execute(
        """
        UPDATE tg_submissions
           SET reactions_count = ?, reactions_json = ?,
               reactions_at = datetime('now'), updated_at = datetime('now')
         WHERE submission_id = ?
        """,
        (total, json.dumps(simple, ensure_ascii=False), sid),
    )
    if not cur.rowcount:
        return False

    row = conn.execute(
        "SELECT account_id FROM tg_submissions WHERE submission_id = ?", (sid,)
    ).fetchone()
    conn.execute(
        "INSERT INTO tg_snapshots (account_id, submission_id, reactions_count)"
        " VALUES (?, ?, ?)",
        (row[0] if row else 0, sid, total),
    )
    return True


def get_submissions(conn: sqlite3.Connection, account_id: int | None = None,
                    limit: int = 200) -> list[dict]:
    q = "SELECT * FROM tg_submissions"
    params: list = []
    if account_id is not None:
        q += " WHERE account_id = ?"
        params.append(account_id)
    q += " ORDER BY posted_at DESC, message_id DESC LIMIT ?"
    params.append(limit)
    return [dict(r) for r in conn.execute(q, params).fetchall()]


def get_summary(conn: sqlite3.Connection, account_id: int | None = None) -> dict:
    """Totals for the dashboard.

    ``uncounted`` is reported alongside the totals on purpose: it is the number
    of posts sent before reaction tracking began, which can never be filled in.
    Without it, a low reaction total looks like poor engagement rather than a
    short observation window.
    """
    where, params = "", []
    if account_id is not None:
        where, params = " WHERE account_id = ?", [account_id]
    row = conn.execute(
        f"""
        SELECT COUNT(*)                                        AS submissions,
               COALESCE(SUM(reactions_count), 0)               AS reactions,
               SUM(CASE WHEN reactions_at IS NULL THEN 1 ELSE 0 END) AS uncounted
          FROM tg_submissions{where}
        """,
        params,
    ).fetchone()
    return {"submissions": row[0] or 0, "reactions": row[1] or 0,
            "uncounted": row[2] or 0}


def get_snapshots(conn: sqlite3.Connection, submission_id: str,
                  limit: int = 500) -> list[dict]:
    return [dict(r) for r in conn.execute(
        "SELECT * FROM tg_snapshots WHERE submission_id = ?"
        " ORDER BY polled_at ASC, id ASC LIMIT ?", (submission_id, limit)).fetchall()]


# -- Telegram Poll Log ----------------------------------------------------------
#
# The cycle fetches only a subscriber count, so `submissions_found` stays 0 and
# `snapshots_inserted` counts follower snapshots rather than per-post ones. The
# table shape matches every other platform's on purpose: /api/platforms/health
# walks them all through a single loop.

def start_tg_poll_log(conn: sqlite3.Connection, account_id: int = 0) -> int:
    cur = conn.execute(
        "INSERT INTO tg_poll_log (started_at, status, account_id)"
        " VALUES (datetime('now'), 'running', ?)", (account_id,))
    conn.commit()
    return cur.lastrowid


def finish_tg_poll_log(conn: sqlite3.Connection, log_id: int, status: str,
                       submissions_found: int = 0, snapshots_inserted: int = 0,
                       error_message: str | None = None,
                       duration_seconds: float = 0) -> None:
    conn.execute(
        """UPDATE tg_poll_log SET finished_at=datetime('now'), status=?,
           submissions_found=?, snapshots_inserted=?, error_message=?,
           duration_seconds=? WHERE id=?""",
        (status, submissions_found, snapshots_inserted, error_message,
         duration_seconds, log_id))
    conn.commit()


def get_tg_last_poll(conn: sqlite3.Connection) -> dict | None:
    row = conn.execute(
        "SELECT * FROM tg_poll_log ORDER BY started_at DESC LIMIT 1").fetchone()
    return dict(row) if row else None


def get_tg_poll_log(conn: sqlite3.Connection, limit: int = 50) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM tg_poll_log ORDER BY started_at DESC LIMIT ?",
        (limit,)).fetchall()
    return [dict(r) for r in rows]


# -- Analytics reads ------------------------------------------------------------
#
# The shapes below mirror every other platform's query module so the dashboard,
# compare page and CSV export can treat Telegram like anything else. Two
# differences are deliberate and permanent:
#
#   * There is exactly ONE metric. No views (not in the Bot API at all) and no
#     comments (channel discussion lives in a linked group, which is a separate
#     chat we do not read). Every response therefore carries `reactions_count`
#     and nothing else — no zero-filled `views` column pretending otherwise.
#   * `uncounted` rides along with the totals. A post from before reaction
#     tracking started can never be filled in, and without that number a small
#     reaction total reads as poor engagement rather than a short window.

def get_submission(conn: sqlite3.Connection, submission_id: str) -> dict | None:
    row = conn.execute("SELECT * FROM tg_submissions WHERE submission_id = ?",
                       (submission_id,)).fetchone()
    return dict(row) if row else None


def get_all_submissions(conn: sqlite3.Connection, sort_by: str = "posted_at",
                        order: str = "desc",
                        account_id: int | None = None) -> list[dict]:
    """All posts, sorted. ``sort_by`` is validated against a fixed set rather
    than interpolated — it arrives from a query string."""
    from database.scope import account_clause
    allowed = {"posted_at", "reactions_count", "title", "message_id",
               "content_type", "updated_at"}
    col = sort_by if sort_by in allowed else "posted_at"
    direction = "ASC" if str(order).lower() == "asc" else "DESC"
    sql = "SELECT * FROM tg_submissions"
    acc_sql, params = account_clause(account_id)
    if acc_sql:
        sql += " WHERE " + acc_sql
    sql += f" ORDER BY {col} {direction}, message_id {direction}"
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def get_deltas(conn: sqlite3.Connection) -> dict[str, dict]:
    """Change in reactions since the previous snapshot, per post.

    Telegram sends the ABSOLUTE reaction state, so a delta here can legitimately
    be negative — someone removing a reaction is real data, not a glitch, and it
    is not clamped to zero.
    """
    # ⚠ The tie-break on `id` is load-bearing. polled_at has one-second
    # resolution and reaction updates are PUSHED, so several can land in the
    # same second — ordering by polled_at alone then picks "latest" and
    # "previous" arbitrarily among the tied rows, and the delta comes out as a
    # random sign. It rendered as every post having LOST reactions. `id` is
    # AUTOINCREMENT, so it is the real arrival order.
    rows = conn.execute(
        """
        SELECT submission_id, reactions_count, polled_at,
               ROW_NUMBER() OVER (PARTITION BY submission_id
                                  ORDER BY polled_at DESC, id DESC) AS rn
          FROM tg_snapshots
        """
    ).fetchall()
    latest: dict[str, int] = {}
    previous: dict[str, int] = {}
    for r in rows:
        if r["rn"] == 1:
            latest[r["submission_id"]] = r["reactions_count"] or 0
        elif r["rn"] == 2:
            previous[r["submission_id"]] = r["reactions_count"] or 0
    return {sid: {"reactions_delta": n - previous.get(sid, n)}
            for sid, n in latest.items()}


def get_aggregate_snapshots(conn: sqlite3.Connection, start: str | None = None,
                            end: str | None = None,
                            account_id: int | None = None) -> list[dict]:
    from database.scope import account_clause
    sql = ("SELECT polled_at, SUM(reactions_count) AS reactions_count"
           " FROM tg_snapshots")
    params: list = []
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


def get_comparison_snapshots(conn: sqlite3.Connection, submission_ids: list[str],
                             start: str | None = None,
                             end: str | None = None) -> dict[str, list[dict]]:
    result: dict[str, list[dict]] = {sid: [] for sid in submission_ids}
    if not submission_ids:
        return result
    placeholders = ",".join("?" * len(submission_ids))
    sql = f"SELECT * FROM tg_snapshots WHERE submission_id IN ({placeholders})"
    params: list = list(submission_ids)
    if start:
        sql += " AND polled_at >= ?"
        params.append(start)
    if end:
        sql += " AND polled_at <= ?"
        params.append(end)
    sql += " ORDER BY polled_at ASC, id ASC"
    for r in conn.execute(sql, params).fetchall():
        result.setdefault(r["submission_id"], []).append(dict(r))
    return result


def get_growth_rates(conn: sqlite3.Connection) -> dict:
    """Reactions gained over the last 7 and 30 days.

    Measured from the SNAPSHOT series, not from `reactions_count`, so it
    reflects change over the window rather than the lifetime total. Posts with
    only one snapshot contribute nothing, which is correct: one observation is
    not a rate.
    """
    out = {}
    for label, days in (("7d", 7), ("30d", 30)):
        row = conn.execute(
            """
            SELECT COALESCE(SUM(latest.reactions_count - earliest.reactions_count), 0) AS gained
              FROM (SELECT submission_id, reactions_count,
                           ROW_NUMBER() OVER (PARTITION BY submission_id
                                              ORDER BY polled_at DESC, id DESC) rn
                      FROM tg_snapshots
                     WHERE polled_at >= datetime('now', ?)) latest
              JOIN (SELECT submission_id, reactions_count,
                           ROW_NUMBER() OVER (PARTITION BY submission_id
                                              ORDER BY polled_at ASC, id ASC) rn
                      FROM tg_snapshots
                     WHERE polled_at >= datetime('now', ?)) earliest
                ON latest.submission_id = earliest.submission_id
             WHERE latest.rn = 1 AND earliest.rn = 1
            """,
            (f"-{days} days", f"-{days} days"),
        ).fetchone()
        out[f"reactions_{label}"] = row[0] or 0
    return out


def get_top_reacted(conn: sqlite3.Connection, limit: int = 10,
                    account_id: int | None = None) -> list[dict]:
    """Most-reacted posts.

    Only posts that have actually been OBSERVED are eligible — a post with
    `reactions_at IS NULL` is excluded rather than ranked at zero, because it
    has no measurement, not a measurement of nothing.
    """
    from database.scope import account_clause
    sql = ("SELECT submission_id, title, reactions_count, link, posted_at"
           " FROM tg_submissions WHERE reactions_at IS NOT NULL")
    params: list = []
    acc_sql, acc_params = account_clause(account_id)
    if acc_sql:
        sql += " AND " + acc_sql
        params.extend(acc_params)
    sql += " ORDER BY reactions_count DESC, posted_at DESC LIMIT ?"
    params.append(limit)
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def get_fastest_growing(conn: sqlite3.Connection, hours: int = 24,
                        limit: int = 10) -> list[dict]:
    """Posts that gained the most reactions in the window.

    Reads the snapshot series rather than the lifetime total, so a post that
    was popular last month does not crowd out one that is moving now.
    """
    rows = conn.execute(
        """
        SELECT s.submission_id,
               MAX(s.reactions_count) - MIN(s.reactions_count) AS reactions_gained,
               t.title, t.link
          FROM tg_snapshots s
          JOIN tg_submissions t ON t.submission_id = s.submission_id
         WHERE s.polled_at >= datetime('now', ?)
         GROUP BY s.submission_id
        HAVING reactions_gained > 0
         ORDER BY reactions_gained DESC
         LIMIT ?
        """,
        (f"-{int(hours)} hours", limit),
    ).fetchall()
    return [dict(r) for r in rows]


def get_dashboard_summary(conn: sqlite3.Connection,
                          account_id: int | None = None) -> dict:
    """The shape every platform dashboard expects, plus Telegram's own caveat.

    ``total_*`` / ``top_*`` / ``fastest_growing`` are the conventional keys so
    the dashboard needs no special case. ``uncounted`` is the addition: the
    number of posts sent before reaction tracking began, which can never be
    filled in. Without it a low total reads as poor engagement rather than a
    short observation window, and that is the single most misleading thing this
    platform's data could say.
    """
    base = get_summary(conn, account_id=account_id)
    return {
        "total_submissions": base["submissions"],
        "total_reactions": base["reactions"],
        "uncounted": base["uncounted"],
        "top_reacted": get_top_reacted(conn, account_id=account_id),
        "fastest_growing": get_fastest_growing(conn),
        "growth_rates": get_growth_rates(conn),
    }
