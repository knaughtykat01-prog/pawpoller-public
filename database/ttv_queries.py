"""SQL for Twitch (`ttv`) — spec 045.

Two kinds of data (see database/ttv_schema.sql):
  * channel snapshots — live state + viewers always; followers / subscribers / Bits only with Connect;
  * past broadcasts and clips — upserted by id, never deleted by a poll, a view count per poll.
"""
from __future__ import annotations

import sqlite3
from typing import Any

from database.scope import account_clause


# -- Broadcasts and clips -----------------------------------------------------

def upsert_ttv_item(conn: sqlite3.Connection, item: dict, account_id: int, polled_at: str) -> None:
    conn.execute(
        """INSERT INTO ttv_submissions
           (submission_id, account_id, kind, title, username, link, thumbnail_url, duration_s, views, posted_at,
            stream_id, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
           ON CONFLICT(submission_id) DO UPDATE SET
            title=excluded.title, username=excluded.username, link=excluded.link,
            thumbnail_url=excluded.thumbnail_url, duration_s=excluded.duration_s, views=excluded.views,
            posted_at=COALESCE(NULLIF(excluded.posted_at, ''), ttv_submissions.posted_at),
            stream_id=excluded.stream_id, updated_at=datetime('now')""",
        (item["submission_id"], account_id, item.get("kind", "vod"), item.get("title", ""), item.get("username", ""),
         item.get("link", ""), item.get("thumbnail_url", ""), item.get("duration_s", 0), item.get("views", 0),
         item.get("posted_at"), item.get("stream_id", "")),
    )
    conn.execute("INSERT INTO ttv_snapshots (account_id, submission_id, polled_at, views) VALUES (?, ?, ?, ?)",
                 (account_id, item["submission_id"], polled_at, item.get("views", 0)))


def get_ttv_item(conn: sqlite3.Connection, submission_id: str) -> dict | None:
    row = conn.execute("SELECT * FROM ttv_submissions WHERE submission_id = ?", (submission_id,)).fetchone()
    return dict(row) if row else None


def get_all_ttv_items(conn: sqlite3.Connection, sort_by: str = "posted_at", order: str = "desc",
                      account_id: int | None = None, kind: str = "") -> list[dict]:
    if sort_by not in {"posted_at", "title", "duration_s", "views", "updated_at"}:
        sort_by = "posted_at"
    order_dir = "DESC" if order.lower() == "desc" else "ASC"
    where, params = account_clause(account_id)
    conds = [where] if where else []
    if kind in ("vod", "clip"):
        conds.append("kind = ?")
        params = [*params, kind]
    sql = "SELECT * FROM ttv_submissions" + (" WHERE " + " AND ".join(conds) if conds else "")
    sql += f" ORDER BY {sort_by} {order_dir}"
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


# -- Channel snapshots --------------------------------------------------------

def insert_ttv_channel_snapshot(conn: sqlite3.Connection, account_id: int, ch: dict, polled_at: str) -> None:
    conn.execute(
        """INSERT INTO ttv_channel_snapshots
           (account_id, polled_at, channel, live, viewers, title, game, followers, subscribers, sub_points,
            bits_top, connected)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (account_id, polled_at, ch.get("channel", ""), 1 if ch.get("live") else 0, ch.get("viewers", 0),
         ch.get("title", ""), ch.get("game", ""), ch.get("followers"), ch.get("subscribers"), ch.get("sub_points"),
         ch.get("bits_top"), 1 if ch.get("connected") else 0),
    )


def latest_ttv_channels(conn: sqlite3.Connection, account_id: int | None = None) -> list[dict]:
    where, params = account_clause(account_id, "c")
    rows = conn.execute(
        """SELECT c.* FROM ttv_channel_snapshots c
           JOIN (SELECT account_id, MAX(id) AS m FROM ttv_channel_snapshots GROUP BY account_id) l
             ON l.m = c.id"""
        + (f" WHERE {where}" if where else "") + " ORDER BY c.account_id", params).fetchall()
    return [dict(r) for r in rows]


def get_ttv_aggregate(conn: sqlite3.Connection, start: str | None = None, end: str | None = None,
                      account_id: int | None = None) -> list[dict]:
    """Followers / subscribers / viewers summed across accounts per poll (NULLs add nothing)."""
    sql = ("SELECT polled_at, SUM(viewers) AS viewers, SUM(followers) AS followers, "
           "SUM(subscribers) AS subscribers FROM ttv_channel_snapshots")
    conds, params = [], []
    if start:
        conds.append("polled_at >= ?")
        params.append(start)
    if end:
        conds.append("polled_at <= ?")
        params.append(end)
    acc_sql, acc_params = account_clause(account_id)
    if acc_sql:
        conds.append(acc_sql)
        params.extend(acc_params)
    if conds:
        sql += " WHERE " + " AND ".join(conds)
    sql += " GROUP BY polled_at ORDER BY polled_at ASC"
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def get_ttv_summary(conn: sqlite3.Connection, account_id: int | None = None) -> dict:
    channels = latest_ttv_channels(conn, account_id)
    where, wp = account_clause(account_id)
    counts = {r["kind"]: r["c"] for r in conn.execute(
        "SELECT kind, COUNT(*) AS c FROM ttv_submissions" + (f" WHERE {where}" if where else "")
        + " GROUP BY kind", wp).fetchall()}
    views = conn.execute("SELECT COALESCE(SUM(views), 0) AS v FROM ttv_submissions"
                         + (f" WHERE {where}" if where else ""), wp).fetchone()["v"]

    def total(field):
        vals = [c[field] for c in channels if c.get(field) is not None]
        return sum(vals) if vals else None

    return {
        "live": any(c["live"] for c in channels),
        "viewers": sum(c["viewers"] for c in channels),
        "followers": total("followers"),
        "subscribers": total("subscribers"),
        "bits_top": total("bits_top"),
        "connected": any(c["connected"] for c in channels),
        "broadcasts": counts.get("vod", 0),
        "clips": counts.get("clip", 0),
        "total_submissions": sum(counts.values()),
        "total_views": views,
        "channels": [{"account_id": c["account_id"], "name": c["channel"], "live": bool(c["live"]),
                      "viewers": c["viewers"], "title": c["title"], "game": c["game"], "followers": c["followers"],
                      "subscribers": c["subscribers"], "connected": bool(c["connected"])} for c in channels],
    }


# -- Poll log -----------------------------------------------------------------

def start_ttv_poll_log(conn: sqlite3.Connection, account_id: int = 0) -> int:
    cur = conn.execute("INSERT INTO ttv_poll_log (started_at, status, account_id) "
                       "VALUES (datetime('now'), 'running', ?)", (account_id,))
    conn.commit()
    return cur.lastrowid


def finish_ttv_poll_log(conn: sqlite3.Connection, log_id: int, status: str, submissions_found: int = 0,
                        snapshots_inserted: int = 0, error_message: str | None = None,
                        duration_seconds: float = 0) -> None:
    conn.execute(
        """UPDATE ttv_poll_log SET finished_at=datetime('now'), status=?, submissions_found=?,
           snapshots_inserted=?, error_message=?, duration_seconds=? WHERE id=?""",
        (status, submissions_found, snapshots_inserted, error_message, duration_seconds, log_id))
    conn.commit()


def get_ttv_last_poll(conn: sqlite3.Connection) -> dict | None:
    row = conn.execute("SELECT * FROM ttv_poll_log ORDER BY started_at DESC LIMIT 1").fetchone()
    return dict(row) if row else None


def get_ttv_poll_log(conn: sqlite3.Connection, limit: int = 50) -> list[dict]:
    return [dict(r) for r in conn.execute(
        "SELECT * FROM ttv_poll_log ORDER BY started_at DESC LIMIT ?", (limit,)).fetchall()]


def get_ttv_channel_snapshots(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    return [dict(r) for r in conn.execute(
        "SELECT * FROM ttv_channel_snapshots ORDER BY polled_at ASC").fetchall()]
