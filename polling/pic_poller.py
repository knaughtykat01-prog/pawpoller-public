"""Picarto (pic) poll cycle — spec 013 (4.46.0).

Two public API calls per account, no login (clients/pic/client.py):
  1. the channel → one pic_channel_snapshots row (lifetime views, followers, subscribers, live state)
     and the shared follower series;
  2. its recordings → pic_submissions, upserted by id, never deleted.

Shaped like polling/yt_poller.py minus tokens and per-item stats (Picarto's recordings carry none).
The network calls happen before any DB write (poller gotcha: no write lock across an await).
"""
from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timezone

import config
from clients.pic.client import PicClient
from database.db import get_connection
from database import pic_queries
from polling.followers import capture_followers
from polling.notifications import describe_error

logger = logging.getLogger(__name__)

pic_poll_progress = {"active": False, "phase": "idle", "current": 0, "total": 0, "message": ""}

_pic_poll_lock = threading.Lock()


def _progress(phase: str, message: str = "", current: int = 0, total: int = 0) -> None:
    pic_poll_progress.update(active=phase not in ("idle", "complete", "error"),
                             phase=phase, current=current, total=total, message=message)


async def run_pic_poll_cycle(account_id: int | None = None, force_full: bool = False,
                             client: PicClient | None = None) -> dict:
    """One Picarto poll for one account. ``client`` is for tests; a real cycle builds its own.

    ``force_full`` is accepted for the shared Full-resync button; every Picarto poll is already full.
    """
    from database import accounts as accounts_db
    _ac = get_connection()
    try:
        if account_id is None:
            account_id = accounts_db.get_default_account_id(_ac, "pic", create=True)
        row = accounts_db.get_account(_ac, account_id)
    finally:
        _ac.close()
    is_default = bool(row["is_default"]) if row else True

    if not _pic_poll_lock.acquire(blocking=False):
        logger.warning("pic poll already running -- skipping (account %s)", account_id)
        return {}
    _progress("starting", "Reading your Picarto channel...")

    conn = None
    log_id = None
    start = time.time()
    stats = {"submissions_found": 0, "snapshots_inserted": 0}
    creds = config.resolve_account_credentials("pic", account_id, is_default, config.get_settings())
    own = client is None
    client = client or PicClient(creds.get("pic_channel", ""))
    try:
        conn = get_connection()
        log_id = pic_queries.start_pic_poll_log(conn, account_id)
        conn.close()
        conn = None

        if not client.channel:
            raise ValueError("No Picarto channel name is set -- add it in Settings → Platforms → Picarto")
        ch = await client.get_channel(refresh=True)
        if not ch:
            raise ValueError(f"Picarto has no channel called {client.channel} -- check the name in Settings")
        _progress("searching", "Reading your recorded streams...")
        recordings = await client.get_videos()

        poll_ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        conn = get_connection()
        pic_queries.insert_pic_channel_snapshot(conn, account_id, ch, poll_ts)
        stats["snapshots_inserted"] = 1
        for rec in recordings:
            pic_queries.upsert_pic_recording(conn, rec, account_id)
        stats["submissions_found"] = len(recordings)
        conn.commit()

        await capture_followers(client, account_id, conn)     # cached channel: no extra request

        duration = time.time() - start
        _progress("complete", f"Done -- {ch['name']}: {ch['views']} views, {ch['followers']} followers, "
                              f"{len(recordings)} recordings")
        pic_queries.finish_pic_poll_log(conn, log_id, "success", duration_seconds=duration, **stats)
        logger.info("pic poll complete in %.1fs -- %d recordings, channel snapshot written",
                    duration, len(recordings))
        return stats
    except Exception as e:
        duration = time.time() - start
        _progress("error", describe_error(e))
        logger.error("pic poll failed: %s", describe_error(e), exc_info=True)
        if log_id:
            c = conn or get_connection()
            try:
                pic_queries.finish_pic_poll_log(c, log_id, "error", error_message=describe_error(e),
                                                duration_seconds=duration, **stats)
            finally:
                if c is not conn:
                    c.close()
        from polling.telegram import send_poll_error
        try:
            await send_poll_error("pic", e)
        except Exception:
            logger.debug("Error alert send failed", exc_info=True)
        raise
    finally:
        _pic_poll_lock.release()
        if conn:
            conn.close()
        if own:
            await client.close()
