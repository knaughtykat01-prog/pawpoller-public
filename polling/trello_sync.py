"""The scheduled Trello sync (spec 005).

A daemon thread on the same pattern as the posting scheduler, the digest scheduler
and the auto-backup scheduler — three of this exact shape already run in
`main.py`, so a fourth costs one thread and no new concepts.

⚠ The scheduled run **applies**; it is not a preview. That is safe only because
the first sync against a board always previews regardless of how it was called
(`trello/sync.py` enforces it), so the schedule can never be the thing that makes
the first, unreviewed write. Until the operator has confirmed a preview once,
every scheduled tick previews and changes nothing.
"""
from __future__ import annotations

import logging
import time

import config
from database.db import get_connection
from trello import mapping, sync

logger = logging.getLogger(__name__)

STARTUP_DELAY = 120       # let settings seed, as the auto-backup scheduler does
MIN_INTERVAL_MIN = 5      # a tighter interval is a rate limit waiting to happen
TICK = 60


def _due(last_run: float, interval_min: int) -> bool:
    return (time.time() - last_run) >= max(MIN_INTERVAL_MIN, interval_min) * 60


def run_trello_scheduler() -> None:
    """Blocking daemon entry point."""
    time.sleep(STARTUP_DELAY)
    last_run = 0.0
    while True:
        try:
            cfg = mapping.get_config()
            interval = int(cfg.get("interval_min") or 0)
            # 0 disables the schedule. Sync-now still works — the operator turning
            # the timer off is not the operator giving up the feature.
            if interval and mapping.is_configured() and mapping.owner_state()[0] \
                    and _due(last_run, interval):
                _tick()
                last_run = time.time()
        except Exception as e:
            # ⚠ Never let a bad tick kill the thread: the next one may well work,
            # and a silently dead scheduler looks exactly like "nothing changed".
            logger.warning("Trello sync tick failed: %s", e)
        time.sleep(TICK)


def _tick() -> None:
    conn = get_connection()
    try:
        report = sync.run_sync(conn, apply=True)
    finally:
        conn.close()
    c = report.get("counts", {})
    # ⚠ Counts only. The report carries client names and prices; those belong on
    # the operator's screen, never in a log file (FR-025).
    if any(c.values()):
        logger.info(
            "Trello sync: %d created, %d moved, %d updated, %d archived, "
            "%d unlinked, %d conflicted, %d skipped",
            c.get("created", 0), c.get("moved", 0), c.get("updated", 0),
            c.get("archived", 0), c.get("unlinked", 0), c.get("conflicted", 0),
            c.get("skipped", 0))
    for err in report.get("errors", []):
        logger.warning("Trello sync: %s", err)
