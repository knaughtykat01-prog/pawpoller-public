"""Desktop-mode pollers: one daemon thread per platform, from the registry.

Replaces the sixteen hand-written ``_start_<x>_poller`` functions that lived in
``main.py`` (~650 lines of the same forty). The list stopped at ``ig``, so
**e621, FurryNetwork, Furbooru and Telegram were polled on the server and never
on the desktop** — nothing errored, they simply never appeared. Four other
hand-written lists had already stopped at the same place
(``documentation_guide.md`` §59.7); this is the fifth.

The registry is ``polling.multi_account.get_poll_cycles()``, which
``tests/test_poll_registry.py`` has asserted against ``accounts.PLATFORMS``,
``DEFAULT_CRED_CHECKS``, ``PLATFORM_NAMES``, the pause toggle and the health
endpoint since 4.0.10. Nothing asserted that the desktop entry point covered
it, because **no test could import ``main.py``** — it pulls pywebview and the
tray. That is why this lives in its own module: so the coverage assertion can
exist (``tests/test_desktop_polling.py``).

Two behaviours the sixteen threads never had, both of them bugs rather than
design (spec ``docs/specs/desktop_polling.md`` §3):

* **per-platform pause** — ``polling_paused_platforms`` was written by the UI
  and read only by the server, so pausing one platform did nothing on desktop;
* **a first poll on launch** — every thread slept a full interval first, so a
  freshly-opened desktop app polled nothing for an hour. The docs described an
  immediate first poll that no thread performed. The server's rule is used
  instead (§8 Q1): poll now if this platform's last completed poll is older
  than its interval, else wait out the remainder.

Everything else is the same shape as before: one event loop per thread (loops
are not thread-safe), settings re-read every iteration so a change in the UI
takes effect on the next cycle, and one platform's failure confined to its own
thread.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
import threading
from datetime import datetime, timezone

import config
from database.platform_metrics import ALL_CODES, setting_key
from polling.multi_account import get_poll_cycles, poll_platform_accounts

logger = logging.getLogger(__name__)

# A mistyped "1" in Settings would otherwise hammer a site every minute; the
# server has floored its interval at 15 minutes since it was written (§8 Q2).
MIN_INTERVAL_MINUTES = 15

# Mirrors server.py's _seconds_until_next: an interval that has nearly elapsed
# counts as due now rather than scheduling a poll for thirty seconds' time.
MIN_STARTUP_DELAY = 300

# Platforms due at launch start a few seconds apart. The server polls them
# concurrently in one cycle, but a desktop machine is also running the UI, the
# tray and pywebview — twenty simultaneous first polls is a visible stall.
STAGGER_SECONDS = 5

_log_table_cache: dict[str, str | None] = {}


def interval_minutes(settings: dict, code: str) -> int:
    """This platform's poll interval, floored. Inkbunny's key is the bare
    ``poll_interval_minutes``; every platform since is ``<code>_…``."""
    raw = settings.get(setting_key(code, "poll_interval_minutes"), 60)
    try:
        return max(MIN_INTERVAL_MINUTES, int(raw))
    except (TypeError, ValueError):
        return 60


def is_paused(settings: dict, code: str) -> bool:
    """Global pause, or this one platform's. The per-platform set is what the
    desktop never read."""
    if settings.get("polling_paused"):
        return True
    return code in set(settings.get("polling_paused_platforms") or [])


def _poll_log_table(conn: sqlite3.Connection, code: str) -> str | None:
    """``<code>_poll_log``, or ``poll_log`` for Inkbunny (it came first).

    Resolved against ``sqlite_master`` rather than assumed, and cached — the
    same shape ``platform_metrics._date_col`` uses.
    """
    if code not in _log_table_cache:
        want = "poll_log" if code == "ib" else f"{code}_poll_log"
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (want,)).fetchone()
        _log_table_cache[code] = want if row else None
    return _log_table_cache[code]


def last_finished_at(code: str) -> str | None:
    """When this platform last FINISHED a poll, from its own poll log.

    The poll log is written by the cycles themselves, so this needs no new
    state and is already correct on the first launch after upgrading — a
    platform polled ten minutes ago will not be re-polled just because the app
    restarted. Any failure reads as "never polled", which errs towards polling.
    """
    try:
        from database.db import get_connection
        conn = get_connection()
        try:
            table = _poll_log_table(conn, code)
            if not table:
                return None
            row = conn.execute(
                f"SELECT MAX(finished_at) FROM {table} WHERE finished_at IS NOT NULL").fetchone()
        finally:
            conn.close()
        return row[0] if row and row[0] else None
    except Exception as e:  # pragma: no cover - defensive
        logger.debug("%s: could not read last poll time: %s", code, e)
        return None


def startup_delay(interval_secs: int, last: str | None, *, now=None) -> float:
    """Seconds to wait before the FIRST poll of a launch — the server's rule.

    ``0`` means due now. An unreadable or absent timestamp is "never polled",
    which is due now.
    """
    if not last:
        return 0
    try:
        dt = datetime.fromisoformat(str(last).replace("Z", "+00:00").replace(" ", "T", 1))
    except (TypeError, ValueError):
        return 0
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    elapsed = ((now or datetime.now(timezone.utc)) - dt).total_seconds()
    remaining = interval_secs - elapsed
    return remaining if remaining > MIN_STARTUP_DELAY else 0


async def poller_loop(code, run_cycle, *, sleep=asyncio.sleep,
                      get_settings=config.get_settings, last_poll=last_finished_at,
                      stagger: float = 0) -> None:
    """One platform's poll loop, for the life of the app.

    ``sleep``, ``get_settings`` and ``last_poll`` are injectable so the pause,
    interval and startup rules can be tested on a fake clock without threads.
    """
    logger.info("%s poller loop started", code.upper())
    interval = interval_minutes(get_settings(), code) * 60
    wait = startup_delay(interval, last_poll(code))
    if wait:
        logger.info("%s: last poll was recent — next in %d min", code.upper(), int(wait // 60))
    else:
        wait = stagger   # due now; spread the launch burst
    while True:
        if wait > 0:
            await sleep(wait)
        settings = get_settings()
        if is_paused(settings, code):
            logger.info("%s poll skipped — polling is paused", code.upper())
        else:
            try:
                await poll_platform_accounts(code, run_cycle=run_cycle)
            except Exception as e:
                # One platform's failure must never kill its thread, nor any other's.
                logger.error("Scheduled %s poll failed: %s", code.upper(), e)
        wait = interval_minutes(get_settings(), code) * 60


def _thread_main(code, run_cycle, stagger: float) -> None:
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(poller_loop(code, run_cycle, stagger=stagger))
    except Exception as e:
        logger.debug("%s poller thread exiting: %s", code.upper(), e)   # daemon teardown


def codes() -> list[str]:
    """Every platform the desktop polls: the registry, in the UI's order.

    Registry order, not ``ALL_CODES`` order, is the authority for membership —
    a platform in the registry but missing from ``ALL_CODES`` must still be
    polled (and would fail ``tests/test_poll_registry.py`` separately).
    """
    cycles = get_poll_cycles()
    ordered = [c for c in ALL_CODES if c in cycles]
    return ordered + sorted(set(cycles) - set(ordered))


def start_all(thread_factory=threading.Thread) -> list[str]:
    """Start one daemon thread per registry platform. Returns the codes started."""
    cycles = get_poll_cycles()
    started: list[str] = []
    for i, code in enumerate(codes()):
        thread_factory(target=_thread_main, args=(code, cycles[code], i * STAGGER_SECONDS),
                       daemon=True, name=f"{code}-poller").start()
        started.append(code)
    return started
