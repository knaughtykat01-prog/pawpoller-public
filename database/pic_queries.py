"""SQL for Picarto (`pic`) — spec 013 (4.46.0).

Two kinds of data (see database/pic_schema.sql):
  * channel snapshots — the numbers: lifetime views, followers, subscribers;
  * recordings — inventory (title, date, length, adult), never deleted by a poll.
"""
from __future__ import annotations

import sqlite3
from typing import Any

from database.scope import account_clause


# -- Recordings ---------------------------------------------------------------

def upsert_pic_recording(conn: sqlite3.Connection, rec: dict, account_id: int) -> None:
    conn.execute(
        """INSERT INTO pic_submissions
           (submission_id, account_id, title, username, link, thumbnail_url, duration_ms, adult, posted_at,
            updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
           ON CONFLICT(submission_id) DO UPDATE SET
            title=excluded.title, username=excluded.username, link=excluded.link,
            thumbnail_url=excluded.thumbnail_url, duration_ms=excluded.duration_ms, adult=excluded.adult,
            posted_at=COALESCE(NULLIF(excluded.posted_at, ''), pic_submissions.posted_at),
            updated_at=datetime('now')""",
        (rec["submission_id"], account_id, rec.get("title", ""), rec.get("username", ""), rec.get("link", ""),
         rec.get("thumbnail_url", ""), rec.get("duration_ms", 0), rec.get("adult", 0), rec.get("posted_at")),
    )


def get_pic_recording(conn: sqlite3.Connection, submission_id: str) -> dict | None:
    row = conn.execute("SELECT * FROM pic_submissions WHERE submission_id = ?", (submission_id,)).fetchone()
    return dict(row) if row else None


def get_all_pic_recordings(conn: sqlite3.Connection, sort_by: str = "posted_at", order: str = "desc",
                           account_id: int | None = None) -> list[dict]:
    if sort_by not in {"posted_at", "title", "duration_ms", "updated_at"}:
        sort_by = "posted_at"
    order_dir = "DESC" if order.lower() == "desc" else "ASC"
    where, params = account_clause(account_id)
    sql = "SELECT * FROM pic_submissions" + (f" WHERE {where}" if where else "")
    sql += f" ORDER BY {sort_by} {order_dir}"
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


# -- Channel snapshots --------------------------------------------------------

def insert_pic_channel_snapshot(conn: sqlite3.Connection, account_id: int, ch: dict, polled_at: str) -> None:
    conn.execute(
        """INSERT INTO pic_channel_snapshots
           (account_id, polled_at, channel, views, followers, subscribers, online, viewers, last_live,
            recordings_enabled)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (account_id, polled_at, ch.get("name", ""), ch.get("views", 0), ch.get("followers", 0),
         ch.get("subscribers", 0), 1 if ch.get("online") else 0, ch.get("viewers", 0), ch.get("last_live"),
         1 if ch.get("recordings") else 0),
    )


def latest_pic_channels(conn: sqlite3.Connection, account_id: int | None = None) -> list[dict]:
    """The newest snapshot of each account's channel — by id, so two polls in one second count once."""
    where, params = account_clause(account_id, "c")
    rows = conn.execute(
        """SELECT c.* FROM pic_channel_snapshots c
           JOIN (SELECT account_id, MAX(id) AS m FROM pic_channel_snapshots GROUP BY account_id) l
             ON l.m = c.id"""
        + (f" WHERE {where}" if where else "") + " ORDER BY c.account_id", params).fetchall()
    return [dict(r) for r in rows]


def get_pic_aggregate(conn: sqlite3.Connection, start: str | None = None, end: str | None = None,
                      account_id: int | None = None) -> list[dict]:
    """Channel numbers summed across accounts per poll, for the standard aggregate chart."""
    sql = ("SELECT polled_at, SUM(views) AS views, SUM(followers) AS followers, "
           "SUM(subscribers) AS subscribers FROM pic_channel_snapshots")
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


def get_pic_growth_rates(conn: sqlite3.Connection, account_id: int | None = None) -> dict:
    """Views and followers gained per day over 24h / 7d / 30d, per account then summed."""
    latest = latest_pic_channels(conn, account_id)
    rates: dict[str, dict] = {}
    for label, hours in (("24h", 24), ("7d", 168), ("30d", 720)):
        dv = df = 0
        seen = False
        for ch in latest:
            past = conn.execute(
                "SELECT views, followers FROM pic_channel_snapshots WHERE account_id = ? "
                "AND polled_at <= datetime('now', ? || ' hours') ORDER BY polled_at DESC LIMIT 1",
                (ch["account_id"], str(-hours))).fetchone()
            if past is None:
                continue
            seen = True
            dv += ch["views"] - past["views"]
            df += ch["followers"] - past["followers"]
        days = hours / 24.0
        rates[label] = {"views_per_day": round(dv / days, 2) if seen else None,
                        "followers_per_day": round(df / days, 2) if seen else None}
    return rates


def get_pic_summary(conn: sqlite3.Connection, account_id: int | None = None) -> dict:
    channels = latest_pic_channels(conn, account_id)
    where, wp = account_clause(account_id)
    recs = conn.execute("SELECT COUNT(*) AS c FROM pic_submissions" + (f" WHERE {where}" if where else ""),
                        wp).fetchone()["c"]
    lives = [c["last_live"] for c in channels if c.get("last_live")]
    return {
        "total_views": sum(c["views"] for c in channels),
        "followers": sum(c["followers"] for c in channels),
        "subscribers": sum(c["subscribers"] for c in channels),
        "total_submissions": recs,
        "online": any(c["online"] for c in channels),
        "last_live": max(lives) if lives else None,
        "channels": [{"account_id": c["account_id"], "name": c["channel"], "views": c["views"],
                      "followers": c["followers"], "subscribers": c["subscribers"], "online": bool(c["online"]),
                      "last_live": c["last_live"], "recordings_enabled": bool(c["recordings_enabled"])}
                     for c in channels],
    }


# -- Poll log -----------------------------------------------------------------

def start_pic_poll_log(conn: sqlite3.Connection, account_id: int = 0) -> int:
    cur = conn.execute("INSERT INTO pic_poll_log (started_at, status, account_id) "
                       "VALUES (datetime('now'), 'running', ?)", (account_id,))
    conn.commit()
    return cur.lastrowid


def finish_pic_poll_log(conn: sqlite3.Connection, log_id: int, status: str, submissions_found: int = 0,
                        snapshots_inserted: int = 0, error_message: str | None = None,
                        duration_seconds: float = 0) -> None:
    conn.execute(
        """UPDATE pic_poll_log SET finished_at=datetime('now'), status=?, submissions_found=?,
           snapshots_inserted=?, error_message=?, duration_seconds=? WHERE id=?""",
        (status, submissions_found, snapshots_inserted, error_message, duration_seconds, log_id))
    conn.commit()


def get_pic_last_poll(conn: sqlite3.Connection) -> dict | None:
    row = conn.execute("SELECT * FROM pic_poll_log ORDER BY started_at DESC LIMIT 1").fetchone()
    return dict(row) if row else None


def get_pic_poll_log(conn: sqlite3.Connection, limit: int = 50) -> list[dict]:
    return [dict(r) for r in conn.execute(
        "SELECT * FROM pic_poll_log ORDER BY started_at DESC LIMIT ?", (limit,)).fetchall()]


def get_pic_channel_snapshots(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    return [dict(r) for r in conn.execute(
        "SELECT * FROM pic_channel_snapshots ORDER BY polled_at ASC").fetchall()]
