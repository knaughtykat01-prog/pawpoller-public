"""Rate watch (4.74.0, spec 039): how often each site turned this PawPoller away.

Every site already writes one poll-log row per check (X: one per account check), with
its error text. This reads those rows; nothing is hooked into the pollers. A fixed
pattern table sorts each row (research R1, built from the VM's real logs):

* **challenged** — a Cloudflare bot check
* **limited**    — too many requests / rate-limited
* **blocked**    — the HTTP library's own ``'403 Forbidden'``, or a poller saying it was refused
* **clear**      — a successful check
* ``None``       — not counted: expired logins (by far the most common error), site errors,
  network errors, a check still running. Counting a plain 403 or "auth failed" would
  alert all day, so a poller that is refused must say so (see documentation_guide).

Counts only: no handles, URLs or message text leave this module (FR-003).
"""
from __future__ import annotations

import re
import sqlite3
from datetime import datetime, timedelta, timezone

from polling.last_check import skip_times

_CHALLENGED = re.compile(r"cloudflare|cf-mitigated|bot check|just a moment", re.I)
_LIMITED = re.compile(r"\b429\b|too many requests|rate.?limit|throttl", re.I)
_BLOCKED = re.compile(r"'403 Forbidden'|refused the sign-in \(HTTP (403|525)\)|blocking automated login", re.I)

KINDS = ("limited", "blocked", "challenged")
ROW_CAP = 2000   # per site per read: a guard, a 4 h seat writes ~42 a week (X: one per account)


def classify(status: str | None, message: str | None) -> str | None:
    status = (status or "").lower()
    msg = message or ""
    if status == "success":
        return "clear"
    if status == "running" or not msg:
        return None
    if _CHALLENGED.search(msg):
        return "challenged"
    if _LIMITED.search(msg):
        return "limited"
    if _BLOCKED.search(msg):
        return "blocked"
    return None


def _log_table(code: str) -> str:
    return "poll_log" if code == "ib" else f"{code}_poll_log"


def _db_time(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def pressure(conn: sqlite3.Connection, sites, now: datetime | None = None,
             since: str | None = None) -> list[dict]:
    """Per-site counts for the details answer (contracts/details-pressure.md).

    ``since`` (4.77.3): when a move was brought in. The moved library carries the OLD copy's
    poll log, and this seat never made those checks: counting them alerted "turned seat
    away 3 checks in a row" minutes after a move, before the seat had checked anything.
    """
    now = now or datetime.now(timezone.utc)
    week, day = now - timedelta(days=7), now - timedelta(days=1)
    try:
        moved = datetime.fromisoformat(str(since).replace("Z", "+00:00")) if since else None
    except ValueError:
        moved = None
    if moved and moved.tzinfo and moved > week:
        week = moved
        day = max(day, moved)
    day_db, day_iso = _db_time(day), day.strftime("%Y-%m-%dT%H:%M:%SZ")
    skipped = skip_times(conn, week.strftime("%Y-%m-%dT%H:%M:%SZ"))
    out = []
    for code in sites:
        try:
            rows = conn.execute(
                f"SELECT status, error_message, replace(started_at, 'T', ' ') FROM {_log_table(code)} "
                "WHERE replace(started_at, 'T', ' ') >= ? ORDER BY id DESC LIMIT ?",
                (_db_time(week), ROW_CAP)).fetchall()
        except sqlite3.Error:
            rows = []   # this install never made the site's tables
        p = {"site": code, "checks_24h": 0, "away_24h": 0, "checks_7d": 0, "away_7d": 0,
             "skips_24h": 0, "skips_7d": 0, "away_in_a_row": 0, "kinds_7d": dict.fromkeys(KINDS, 0)}
        streak_open = True
        for status, msg, at in rows:   # newest first
            kind = classify(status, msg)
            if kind is None:
                continue
            recent = (at or "") >= day_db
            p["checks_7d"] += 1
            p["checks_24h"] += recent
            if kind == "clear":
                streak_open = False
                continue
            p["away_7d"] += 1
            p["away_24h"] += recent
            p["kinds_7d"][kind] += 1
            if streak_open:
                p["away_in_a_row"] += 1
        times = skipped.get(code, [])
        p["skips_7d"] = len(times)
        p["skips_24h"] = sum(1 for t in times if t >= day_iso)
        if p["checks_7d"] or p["skips_7d"]:
            out.append(p)
    return out
