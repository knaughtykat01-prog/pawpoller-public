"""Background drift detection for a paired desktop (3.18.0).

Answers "is there anything new on the server?" without moving a byte and
without touching anything locally.

⚠ **This detects. It never applies.** Auto-sync was switched off on the desktop
after pairing corrupted four server accounts through offset `account_id`s. That
cause is fixed (3.5.4), but the reason for caution was never the specific bug —
it was that a silent bidirectional process can damage the catalogue faster than
a person notices, and this catalogue is not reconstructible. Telling someone
what changed is safe; changing it for them is a different decision, and it stays
theirs.

Cheap enough to run on a timer: hashing the whole 171 MB artwork tree takes
~0.5s warm, because the comparison is bounded by the OS page cache rather than
by disk. The remote side is one manifest request.
"""
from __future__ import annotations

import asyncio
import logging
import threading
import time

import config

logger = logging.getLogger(__name__)

# Default cadence. Long on purpose: the thing being watched changes when a
# person does something on the server, not continuously, and a paired desktop
# is often left running for days.
_DEFAULT_INTERVAL_MINUTES = 30

# Last known answer, served to the UI so a badge costs nothing to render.
STATE: dict = {
    "checked_at": "",
    "in_sync": None,          # None = never checked
    "files_to_fetch": 0,
    "bytes_to_fetch": 0,
    "error": "",
}

_stop = threading.Event()


def _interval_seconds() -> int:
    try:
        minutes = int(config.get_settings().get("mirror_check_interval_minutes", 0))
    except (TypeError, ValueError):
        minutes = 0
    return max(300, (minutes or _DEFAULT_INTERVAL_MINUTES) * 60)


def _should_run() -> bool:
    """Three conditions, all of which can change while the app is running.

    Re-read every tick rather than captured at startup, so turning the toggle
    on takes effect without a restart — a setting that needs a relaunch to
    apply is the kind of thing people conclude is broken.
    """
    settings = config.get_settings()
    if not settings.get("mirror_auto_check"):
        return False
    if not settings.get("posting_server_url"):
        return False
    try:
        from posting.scheduler import detect_runtime_mode
        if detect_runtime_mode() == "server":
            # The server is the source of truth; there is nothing above it to
            # be out of date with.
            return False
    except Exception:  # noqa: BLE001
        return False
    return True


async def check_once() -> dict:
    """One drift check, recorded into STATE. Never raises."""
    from routes.mirror_api import compute_drift
    try:
        d = await compute_drift()
        STATE.update({
            "checked_at": d.get("checked_at", ""),
            "in_sync": bool(d.get("in_sync")),
            "files_to_fetch": d.get("files_to_fetch", 0) or d.get("folders_to_fetch", 0),
            "bytes_to_fetch": d.get("bytes_to_fetch", 0),
            "error": "",
        })
        if not d.get("in_sync"):
            logger.info("Mirror watcher: %s file(s) out of date (%s bytes)",
                        STATE["files_to_fetch"], STATE["bytes_to_fetch"])
    except Exception as e:  # noqa: BLE001 — a watcher must never take the app down
        STATE.update({"error": str(e), "checked_at": ""})
        logger.debug("Mirror watcher: check failed: %s", e)
    return dict(STATE)


def run_drift_watcher() -> None:
    """Thread entry point. Registered in `server.py` beside the other daemons."""
    # Settle first: at startup everything else is competing for disk, and a
    # drift check is the least urgent thing happening.
    if _stop.wait(120):
        return
    while not _stop.is_set():
        if _should_run():
            try:
                asyncio.run(check_once())
            except Exception as e:  # noqa: BLE001
                logger.debug("Mirror watcher: tick failed: %s", e)
        # Poll the toggle on a short clock even when idle, so enabling it does
        # not wait a full interval to take effect.
        waited = 0
        step = 60
        target = _interval_seconds()
        while waited < target and not _stop.is_set():
            if _stop.wait(step):
                return
            waited += step
            if not _should_run():
                break


def stop() -> None:
    _stop.set()
