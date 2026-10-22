"""Experience points and levels (XPLEVELS, 4.84.0, spec 050 Part 1).

Every number shown is a sum over ``xp_ledger``, one row per award. There is one
write path, :func:`award`: it applies the daily caps and gives each award once,
keyed by a UNIQUE ``award_key``.

Most XP is *swept* from ``posting_log`` (:func:`sync`), which every posting path
already writes, so no poster knows XP exists, and the one-off backfill for
long-time users is that same sweep run once over history (uncapped, marked
``backfill``). The two actions that leave no record of their own, an Inbox
reply and tidying a work, call :func:`award` directly.

Counting only, no AI (constitution I). Actions, never audience size (FR-002).
Design: ``specs/050-xp-levels/research.md``; guide §177.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

import config

# Display groups for "this week by kind" (Laurels).
GROUPS = {"work": "posting", "site": "posting", "streak": "posting",
          "words": "writing", "reply": "community", "tidy": "care",
          "audience": "audience"}   # spec 052: never `live`, so outside the caps and the week

_WORK_TYPES = ("story", "artwork")   # Posts-module microblog rows earn nothing (research R2)


def _table() -> dict:
    return config.XP_TABLE


def _now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _local(at: str) -> datetime:
    """A stored UTC 'YYYY-MM-DD HH:MM:SS' as the operator's local time."""
    dt = datetime.strptime(at[:19].replace("T", " "), "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    return dt.astimezone(config.display_zone())


# ── Level maths: reaching level L takes step/2 · L · (L−1) ─────────────

def level_start(level: int) -> int:
    return _table()["level_step"] * level * (level - 1) // 2


def level_for(xp: int) -> int:
    level = 1
    while level_start(level + 1) <= xp:
        level += 1
    return level


# ── The one write path ──────────────────────────────────────────

def award(conn: sqlite3.Connection, kind: str, key: str, points: int, at: str | None = None,
          site: str | None = None, source: str = "live") -> int:
    """Record one award, once. Returns the points actually granted (0 = already
    given, or a cap took it all). Live rows are clamped to the per-site and daily
    caps; a clamped row is still written (``capped = 1``) so the key is spent and
    the screen can say "daily XP reached". Does not commit."""
    at = at or _now_utc()
    day = _local(at).strftime("%Y-%m-%d")
    if conn.execute("SELECT 1 FROM xp_ledger WHERE award_key = ?", (key,)).fetchone():
        return 0
    granted, capped = int(points), 0
    if source == "live" and granted > 0:
        t = _table()
        left = t["cap_day"] - _sum(conn, "day = ? AND source = 'live'", (day,))
        if kind == "site" and site:
            left = min(left, t["cap_site_day"] - _sum(
                conn, "day = ? AND site = ? AND kind = 'site' AND source = 'live'", (day, site)))
        if left < granted:
            granted, capped = max(left, 0), 1
    conn.execute(
        "INSERT OR IGNORE INTO xp_ledger (award_key, kind, site, points, capped, source, earned_at, day)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (key, kind, site, granted, capped, source, at, day))
    return granted


def _sum(conn, where: str, args: tuple) -> int:
    return conn.execute(f"SELECT COALESCE(SUM(points), 0) FROM xp_ledger WHERE {where}", args).fetchone()[0]


# ── The sweep (and the one-off backfill) ─────────────────────────

def _state(conn, key: str) -> str | None:
    row = conn.execute("SELECT value FROM xp_state WHERE key = ?", (key,)).fetchone()
    return row[0] if row else None


def _set_state(conn, key: str, value: str) -> None:
    conn.execute("INSERT INTO xp_state (key, value) VALUES (?, ?)"
                 " ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, value))


def _words(conn, story_name: str, chapter_index: int) -> int:
    row = conn.execute("SELECT MAX(word_count) FROM publications WHERE story_name = ? AND chapter_index = ?",
                       (story_name, chapter_index)).fetchone()
    return int(row[0] or 0) if row else 0


def _award_post(conn, row, source: str) -> None:
    t = _table()
    ctype, name, plat, at = row["content_type"], row["story_name"], row["platform"], row["created_at"]
    award(conn, "work", f"work:{ctype}:{name}", t["work"], at, source=source)
    award(conn, "site", f"site:{ctype}:{name}:{plat}", t["site"], at, site=plat, source=source)
    if ctype == "story":
        ch = int(row["chapter_index"] or 0)
        thousands = _words(conn, name, ch) // 1000
        if thousands:
            award(conn, "words", f"words:{name}:{ch}", thousands * t["words_per_1000"], at, source=source)
    iso = _local(at).isocalendar()
    award(conn, "streak", f"week:{iso[0]}-W{iso[1]:02d}", t["streak_week"], at, source=source)


def sync(conn: sqlite3.Connection) -> None:
    """Sweep posting_log rows newer than the watermark into the ledger and commit.
    The first run, with no ``backfilled_at`` yet, is the backfill: all history, as
    ``backfill`` rows, uncapped, in one transaction. Re-running is a no-op (US2 #2)."""
    from database import accounts as accounts_db
    source = "live" if _state(conn, "backfilled_at") else "backfill"
    mark = int(_state(conn, "posting_log_watermark") or 0)
    never = accounts_db.never_post_ids()
    prev_factory = conn.row_factory
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT log_id, platform, story_name, chapter_index, account_id, content_type, created_at"
            " FROM posting_log WHERE log_id > ? AND action = 'post' AND status = 'success'"
            f" AND content_type IN ({','.join('?' * len(_WORK_TYPES))}) ORDER BY log_id",
            (mark, *_WORK_TYPES)).fetchall()
        top = conn.execute("SELECT COALESCE(MAX(log_id), 0) FROM posting_log").fetchone()[0]
    finally:
        conn.row_factory = prev_factory
    for row in rows:
        if (row["account_id"] or 0) in never:
            continue
        _award_post(conn, row, source)
    _set_state(conn, "posting_log_watermark", str(max(mark, top)))
    if source == "backfill":
        _set_state(conn, "backfilled_at", _now_utc())
    import rewards
    rewards.unlock_due(conn)          # spec 051: unlocks land in the same commit as the XP
    conn.commit()


# ── What the screen shows (contracts/xp-api.md) ──────────────────

def enabled() -> bool:
    """Gamification (levels, medals, rewards) is a choice, OFF until the person says yes (operator,
    22 Oct). Off hides it all; the ledger and medals keep counting, so turning it on loses nothing."""
    return bool(config.get_settings().get("levels_enabled", False))


def asked() -> bool:
    """Whether the person has answered the question (any saved choice counts)."""
    return "levels_enabled" in config.get_settings()


def totals(conn: sqlite3.Connection) -> dict:
    t = _table()
    xp = _sum(conn, "1 = 1", ())
    level = level_for(xp)
    now = datetime.now(config.display_zone())
    today = now.strftime("%Y-%m-%d")
    monday = (now - timedelta(days=now.weekday())).strftime("%Y-%m-%d")
    by_kind = {g: 0 for g in ("posting", "writing", "community", "care")}
    for kind, pts in conn.execute(
            "SELECT kind, SUM(points) FROM xp_ledger WHERE day >= ? AND source = 'live' GROUP BY kind",
            (monday,)).fetchall():
        by_kind[GROUPS.get(kind, "posting")] += pts or 0
    capped = [r[0] for r in conn.execute(
        "SELECT site FROM xp_ledger WHERE day = ? AND kind = 'site' AND source = 'live'"
        " GROUP BY site HAVING SUM(points) >= ?", (today, t["cap_site_day"])).fetchall()]
    import rewards
    return {
        "enabled": enabled(),
        "xp": xp,
        "level": level,
        "level_start": level_start(level),
        "next_level_at": level_start(level + 1),
        "today": {"xp": _sum(conn, "day = ? AND source = 'live'", (today,)), "cap": t["cap_day"],
                  "sites_capped": capped},
        "week": {"xp": sum(by_kind.values()), "by_kind": by_kind},
        "backfilled_xp": _sum(conn, "source = 'backfill'", ()),
        "audience_xp": _sum(conn, "source = 'audience'", ()),
        "rewards": rewards.block(conn, enabled(), xp, level),
    }
