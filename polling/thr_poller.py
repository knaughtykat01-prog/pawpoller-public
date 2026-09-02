"""Threads (THR) poll cycle orchestration.

Official Threads Graph API (graph.threads.net), OAuth long-lived token.

Key differences from other pollers:
  - Uses ThrClient with access_token + optional target user_id
  - Settings keys: thr_access_token, thr_user_id
  - Metrics: views, likes, reposts, replies, quotes
  - Notifications: "THR:" prefix, thread/balloon emoji
"""

from __future__ import annotations
import atexit
import logging
import threading
import time
from datetime import datetime, timezone
from html import escape as _esc

import config
from clients.thr.client import ThrClient
from database.db import get_connection
from polling.notifications import describe_error
from database import thr_queries
from polling import notifications

logger = logging.getLogger(__name__)

# -- Progress tracking --------------------------------------------------------
thr_poll_progress = {
    "active": False,
    "phase": "idle",
    "current": 0,
    "total": 0,
    "message": "",
}

_thr_poll_running = False
_thr_poll_lock = threading.Lock()
_thr_first_poll_done: set[int] = set()

# Persistent client — reused across poll cycles
_thr_client: ThrClient | None = None


def _cleanup_thr_client():
    if _thr_client is not None:
        import asyncio
        try:
            asyncio.get_event_loop().run_until_complete(_thr_client.close())
        except Exception:
            logger.debug("Error alert send failed", exc_info=True)


atexit.register(_cleanup_thr_client)


def _update_thr_progress(phase: str, current: int = 0, total: int = 0, message: str = ""):
    thr_poll_progress["active"] = phase not in ("idle", "complete", "error")
    thr_poll_progress["phase"] = phase
    thr_poll_progress["current"] = current
    thr_poll_progress["total"] = total
    thr_poll_progress["message"] = message


def _send_thr_notifications(new_details: list[dict]) -> None:
    """Send Windows toast notifications for Threads activity."""
    settings = config.get_settings()
    n = len(new_details)
    notifications.maybe_show_toast(
        settings,
        "thr_notifications_enabled",
        f"THR: {n} Post{'s' if n != 1 else ''} Updated",
        [f"{d['title'][:50]} gained activity" for d in new_details],
    )


async def _send_thr_telegram(new_details: list[dict]) -> None:
    """Send Telegram notification for Threads activity."""
    settings = config.get_settings()
    n = len(new_details)
    await notifications.maybe_send_telegram_summary(
        settings,
        f"<b>\U0001f9f5 THR: {n} Post{'s' if n != 1 else ''} Updated</b>",
        [_esc(d['title'][:50]) for d in new_details],
        log_label="THR",
    )


def _get_or_create_client(settings: dict, thr_access_token: str, thr_user_id: str) -> ThrClient:
    """Return the persistent ThrClient, re-pointed at the account's credentials."""
    global _thr_client

    if _thr_client is None:
        from polling.cf_proxy import proxy_kwargs
        _thr_client = ThrClient(
            access_token=thr_access_token,
            user_id=thr_user_id,
            **proxy_kwargs(settings, "thr"),
        )
    else:
        _thr_client.update_credentials(thr_access_token, thr_user_id)

    return _thr_client


async def run_thr_poll_cycle(account_id: int | None = None, force_full: bool = False) -> dict:
    """Execute one complete Threads poll cycle for a single account."""
    global _thr_poll_running

    from database import accounts as accounts_db
    _ac = get_connection()
    try:
        if account_id is None:
            account_id = accounts_db.get_default_account_id(_ac, "thr", create=True)
        account_row = accounts_db.get_account(_ac, account_id)
    finally:
        _ac.close()
    is_default = bool(account_row["is_default"]) if account_row else True
    is_first = account_id not in _thr_first_poll_done

    if not _thr_poll_lock.acquire(blocking=False):
        logger.warning("THR poll already running -- skipping (account %s)", account_id)
        return {}
    _thr_poll_running = True
    _update_thr_progress("starting", message="Initialising THR poll cycle...")

    conn = None
    log_id = None
    start_time = time.time()

    stats = {
        "submissions_found": 0,
        "snapshots_inserted": 0,
    }

    settings = config.get_settings()
    creds = config.resolve_account_credentials("thr", account_id, is_default, settings)
    client = _get_or_create_client(settings, creds.get("thr_access_token", ""),
                                   creds.get("thr_user_id", ""))

    try:
        conn = get_connection()
        log_id = thr_queries.start_thr_poll_log(conn, account_id)

        # Step 1: Validate token / resolve account
        _update_thr_progress("searching", message="Authenticating with Threads...")
        name = await client.validate_session()
        if not name:
            raise ValueError("Threads auth failed -- check the access token")

        # Step 2: Discover posts
        _update_thr_progress("searching", message="Fetching thread list...")
        post_items = await client.get_all_post_uris()
        stats["submissions_found"] = len(post_items)
        logger.info("THR: Found %d threads", len(post_items))

        if not post_items:
            _update_thr_progress("complete", message="No Threads posts found.")
            thr_queries.finish_thr_poll_log(conn, log_id, "success",
                                            duration_seconds=time.time() - start_time, **stats)
            conn.commit()
            return stats

        # Step 3: Fetch per-post insights
        _update_thr_progress("fetching_details",
                            message=f"Fetching insights for {len(post_items)} posts...")
        details = await client.get_post_details_batch(post_items)
        logger.info("THR: Fetched insights for %d posts", len(details))

        # Step 4: Upsert + snapshot
        new_activity_details: list[dict] = []
        poll_timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

        for idx, detail in enumerate(details, 1):
            _update_thr_progress("processing", current=idx, total=len(details),
                                message=f"Processing post {idx}/{len(details)}...")
            try:
                uri = detail["post_uri"]
                views = detail.get("views", 0)
                likes = detail.get("likes", 0)
                reposts = detail.get("reposts", 0)
                replies = detail.get("replies", 0)
                quotes = detail.get("quotes", 0)

                prev = thr_queries.get_thr_submission(conn, uri)
                if prev and (likes > prev.get("likes", 0)
                             or replies > prev.get("replies", 0)):
                    new_activity_details.append({"title": detail.get("title", "")})

                thr_queries.upsert_thr_submission(conn, detail, account_id)
                thr_queries.insert_thr_snapshot(conn, account_id, uri, views, likes,
                                                reposts, replies, quotes, polled_at=poll_timestamp)
                stats["snapshots_inserted"] += 1

            except Exception as e:
                logger.warning("Error processing THR post %s: %s",
                               detail.get("post_uri", "")[:50], e, exc_info=True)

        conn.commit()

        # ── Notifications ─────────────────────────────────────
        if is_first:
            logger.info("First THR poll for account %s -- suppressing %d activity notifications",
                        account_id, len(new_activity_details))
        else:
            try:
                _send_thr_notifications(new_activity_details)
            except Exception as ne:
                logger.warning("Failed to send THR notifications: %s", ne, exc_info=True)
            try:
                await _send_thr_telegram(new_activity_details)
            except Exception as te:
                logger.warning("Failed to send THR Telegram notification: %s", te, exc_info=True)

        # Finalise
        duration = time.time() - start_time
        _update_thr_progress("complete", current=len(details), total=len(details),
                            message=f"Done -- {stats['submissions_found']} posts in {duration:.1f}s")
        thr_queries.finish_thr_poll_log(conn, log_id, "success",
                                        duration_seconds=duration, **stats)
        logger.info("THR poll complete in %.1fs -- %d posts, %d snapshots",
                     duration, stats["submissions_found"], stats["snapshots_inserted"])

        # -- Telegram notifications ----------------------------------------
        if not is_first:
            from polling.telegram import send_poll_summary, check_milestones_batch, check_goals
            try:
                await send_poll_summary("thr", stats, duration)
            except Exception as te:
                logger.warning("Failed to send THR Telegram summary: %s", te, exc_info=True)
            try:
                await check_milestones_batch("thr", "thr_snapshots", "thr_submissions", account_id)
            except Exception as me:
                logger.warning("Failed to check THR milestones: %s", me, exc_info=True)
            try:
                await check_goals()
            except Exception as ge:
                logger.warning("Failed to check goals: %s", ge, exc_info=True)

        return stats

    except Exception as e:
        duration = time.time() - start_time
        _update_thr_progress("error", message=describe_error(e))
        logger.error("THR poll failed: %s", describe_error(e), exc_info=True)
        if conn and log_id:
            thr_queries.finish_thr_poll_log(conn, log_id, "error",
                                            error_message=describe_error(e),
                                            duration_seconds=duration, **stats)
            conn.commit()
        from polling.telegram import send_poll_error
        try:
            await send_poll_error("thr", e)
        except Exception:
            logger.debug("Error alert send failed", exc_info=True)
        raise
    finally:
        _thr_first_poll_done.add(account_id)
        _thr_poll_running = False
        _thr_poll_lock.release()
        if conn:
            conn.close()
