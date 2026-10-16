"""Sites skipped in their last round (spec 038, research R1), and every skip for 7 days (spec 039).

A skipped turn (spec 037) writes no poll-log row, so ``/api/health/details`` would
show the site's older result as if it were the latest. The latest skip is kept here,
in memory: a restart losing one is harmless, the next round writes a real row.
Rate watch (4.74.0) also counts skips over 7 days, so each one goes in ``turn_skips``.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

logger = logging.getLogger(__name__)

_skips: dict[str, tuple[str, str]] = {}
KEEP_DAYS = 7


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def record_skip(code: str, reason: str, count: bool = True) -> None:
    """*count* False: show the reason, but leave rate watch's skip totals alone (an operator pause, spec 042)."""
    now = datetime.now(timezone.utc)
    _skips[code] = (_iso(now), reason)
    if not count:
        return
    try:
        from database.db import get_connection
        conn = get_connection()
        try:
            conn.execute("INSERT INTO turn_skips (site, at) VALUES (?, ?)", (code, _iso(now)))
            conn.execute("DELETE FROM turn_skips WHERE at < ?", (_iso(now - timedelta(days=KEEP_DAYS)),))
            conn.commit()
        finally:
            conn.close()
    except Exception as e:  # noqa: BLE001 — a lost count must never break the round
        logger.warning("Couldn't record the skip for %s: %s", code, e)


def skips() -> dict[str, tuple[str, str]]:
    return dict(_skips)


def skip_times(conn, since_iso: str) -> dict[str, list[str]]:
    """{site: [iso, …]} for skips at or after *since_iso*."""
    out: dict[str, list[str]] = {}
    try:
        for site, at in conn.execute("SELECT site, at FROM turn_skips WHERE at >= ?", (since_iso,)):
            out.setdefault(site, []).append(at)
    except Exception:  # noqa: BLE001 — table not made yet on an old DB
        pass
    return out
