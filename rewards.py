"""Level rewards (LVLREWARDS, spec 051): titles, gold-level items, surprises.

What unlocks is a lookup in ``config.REWARD_TABLE`` against the level and the
XP ledger, done on the server inside ``xp.sync()`` so a reveal shows once per
install. ``xp_rewards`` is insert-only: a reward is never taken back (FR-012),
and only ``revealed_at`` ever changes. Cosmetic only (FR-003), no randomness,
no AI. Design: ``specs/051-level-rewards/research.md``; guide §177.
"""
from __future__ import annotations

import sqlite3
from datetime import date, datetime, timedelta

import config
import xp


def _table() -> dict:
    return config.REWARD_TABLE


def _by_id() -> dict:
    t = _table()
    return {r["id"]: r for r in (*t["items"], *t["surprises"])}


def title_for(level: int) -> str:
    """The title of the highest title level reached; the top one holds past the table."""
    titles = _table()["titles"]
    return titles[max(k for k in titles if k <= max(level, 1))]


def next_reward(level: int, total_xp: int) -> dict | None:
    """The next gold-level item, or None once they are all earned (US1 #2)."""
    for item in sorted(_table()["items"], key=lambda r: r["level"]):
        if item["level"] > level:
            out = {"kind": item["kind"], "name": item["name"], "level": item["level"],
                   "xp_to_go": xp.level_start(item["level"]) - total_xp}
            if item["level"] in _table()["titles"]:
                out["title"] = _table()["titles"][item["level"]]
            return out
    return None


# ── Surprise checks: fixed queries over xp_ledger (research R4) ──────────

def _streak_weeks(conn) -> int:
    """Longest run of consecutive ISO weeks among the ``week:YYYY-Www`` keys."""
    mondays = sorted(date.fromisocalendar(int(k[5:9]), int(k[11:13]), 1) for (k,) in conn.execute(
        "SELECT award_key FROM xp_ledger WHERE kind = 'streak' AND award_key LIKE 'week:%'"))
    best = run = 0
    for i, d in enumerate(mondays):
        run = run + 1 if i and (d - mondays[i - 1]).days == 7 else 1
        best = max(best, run)
    return best


_CHECKS = {
    "streak_weeks": _streak_weeks,
    "distinct_sites": lambda c: c.execute(
        "SELECT COUNT(DISTINCT site) FROM xp_ledger WHERE kind = 'site'").fetchone()[0],
    "replies": lambda c: c.execute("SELECT COUNT(*) FROM xp_ledger WHERE kind = 'reply'").fetchone()[0],
    "tidies": lambda c: c.execute("SELECT COUNT(*) FROM xp_ledger WHERE kind = 'tidy'").fetchone()[0],
}


def unlock_due(conn: sqlite3.Connection) -> None:
    """Insert a row for every reward now due. The first run marks them all
    ``catchup`` (US5). Never deletes, does not commit."""
    level = xp.level_for(xp._sum(conn, "1 = 1", ()))
    caught_up = xp._state(conn, "rewards_caught_up_at")
    due = [(r["id"], "level") for r in _table()["items"] if r["level"] <= level]
    due += [(sp["id"], "surprise") for sp in _table()["surprises"]
            if _CHECKS[sp["rule"][0]](conn) >= sp["rule"][1]]
    now = xp._now_utc()
    for rid, source in due:
        conn.execute("INSERT OR IGNORE INTO xp_rewards (reward_id, source, unlocked_at) VALUES (?, ?, ?)",
                     (rid, source if caught_up else "catchup", now))
    if not caught_up:
        xp._set_state(conn, "rewards_caught_up_at", now)


def _unlocked(conn) -> list[tuple]:
    return conn.execute("SELECT reward_id, source FROM xp_rewards ORDER BY unlocked_at, reward_id").fetchall()


def unlocked_ids(conn) -> set[str]:
    return {rid for rid, _ in _unlocked(conn)}


def unlocked_theme_ids(conn) -> set[str]:
    """Theme preference values the server accepts beyond the built-ins (FR-014)."""
    return {rid[len("theme_"):] for rid in unlocked_ids(conn) if rid.startswith("theme_")}


def catalog(unlocked_ids) -> list[dict]:
    """Everything on the shelf. An unfound surprise shows its hint, not its name;
    an unlocked id no longer in the table stays, marked retired."""
    unlocked_ids = set(unlocked_ids)
    out = [dict(r) for r in _table()["items"]]
    for sp in _table()["surprises"]:
        row = {"id": sp["id"], "kind": sp["kind"], "hint": sp["hint"], "surprise": True}
        if sp["id"] in unlocked_ids:
            row["name"] = sp["name"]
        out.append(row)
    known = _by_id()
    out += [{"id": rid, "kind": _kind_of(rid), "name": rid, "retired": True}
            for rid in sorted(unlocked_ids - known.keys())]
    return out


def _kind_of(rid: str) -> str:
    """Kind of a retired id, from its prefix (ids are never reused)."""
    return "theme" if rid.startswith("theme_") else "style" if rid.startswith("lv_") else "ring"


def equipped(conn) -> dict:
    """Ring and style in use, dropping anything not unlocked; plus the theme preference."""
    s = config.get_settings()
    have = unlocked_ids(conn)
    eq = s.get("rewards_equipped") or {}
    return {k: (eq.get(k) if eq.get(k) in have else None) for k in ("ring", "style")} | {"theme": s.get("theme")}


def avatar(conn) -> dict | None:
    row = conn.execute("SELECT source, work_name, rating, updated_at FROM profile_picture WHERE id = 1").fetchone()
    return dict(zip(("source", "work_name", "rating", "updated_at"), row)) if row else None


def block(conn: sqlite3.Connection, enabled: bool, total_xp: int, level: int) -> dict:
    """The ``rewards`` part of ``GET /api/xp`` (contracts/rewards-api.md)."""
    rows = _unlocked(conn)
    known = _by_id()
    unrevealed = []
    if enabled:
        for rid, source in conn.execute(
                "SELECT reward_id, source FROM xp_rewards WHERE revealed_at IS NULL ORDER BY unlocked_at, reward_id"):
            r = known.get(rid, {"kind": _kind_of(rid), "name": rid})
            unrevealed.append({"id": rid, "kind": r["kind"], "name": r["name"], "source": source,
                               "level": r.get("level")})
        unrevealed.sort(key=lambda u: u["level"] or 999)   # level order; surprises last
    return {
        "title": title_for(level),
        # The level the title was earned at, so the level-up pop can say "New title" (US2).
        "title_level": max(k for k in _table()["titles"] if k <= max(level, 1)),
        "next": next_reward(level, total_xp) if enabled else None,
        "equipped": equipped(conn),
        "unlocked": [rid for rid, _ in rows],
        "unrevealed": unrevealed,
        "catalog": catalog(rid for rid, _ in rows),
        "avatar": avatar(conn),
    }


def history(conn: sqlite3.Connection, now: datetime | None = None) -> dict:
    """The date each level was first reached (a running total over the ledger in award
    order), and XP this month against last month, both in the display zone (US7)."""
    levels, total, nxt = [], 0, 2
    for points, at in conn.execute("SELECT points, earned_at FROM xp_ledger ORDER BY earned_at, id"):
        total += points
        while xp.level_start(nxt) <= total:
            levels.append({"level": nxt, "date": xp._local(at).strftime("%Y-%m-%d")})
            nxt += 1
    now = now or datetime.now(config.display_zone())
    first = now.replace(day=1)
    prev = (first - timedelta(days=1)).replace(day=1)
    month = lambda a, b: xp._sum(conn, "day >= ? AND day < ?", (a.strftime("%Y-%m-%d"), b.strftime("%Y-%m-%d")))
    return {"levels": levels,
            "month": {"this": month(first, (first + timedelta(days=32)).replace(day=1)), "last": month(prev, first),
                      "this_label": first.strftime("%B"), "last_label": prev.strftime("%B")}}


def reveal(conn: sqlite3.Connection, ids) -> int:
    """Mark rows shown, once, for the whole install. Idempotent; commits."""
    ids = [str(i) for i in ids or []][:100]
    if not ids:
        return 0
    n = conn.execute(f"UPDATE xp_rewards SET revealed_at = ? WHERE revealed_at IS NULL"
                     f" AND reward_id IN ({','.join('?' * len(ids))})", (xp._now_utc(), *ids)).rowcount
    conn.commit()
    return n
