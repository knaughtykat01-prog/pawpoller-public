"""Tumblr (TUM) poll cycle orchestration.

Read-only polling via the Tumblr v2 API (API key + blog identifier).

Key differences from other pollers:
  - Uses TumClient with api_key + blog
  - Settings keys: tum_api_key, tum_blog
  - Single metric: notes (note_count = likes + reblogs + replies combined)
  - Notifications: "TUM:" prefix, blue-book emoji
"""

from __future__ import annotations
import atexit
import logging
import threading
import time
from datetime import datetime, timezone
from html import escape as _esc

import config
from clients.tum.client import TumClient
from database.db import get_connection
from polling.notifications import describe_error
from database import tum_queries
from polling import notifications

logger = logging.getLogger(__name__)

# -- Progress tracking --------------------------------------------------------
tum_poll_progress = {
    "active": False,
    "phase": "idle",
    "current": 0,
    "total": 0,
    "message": "",
}

_tum_poll_running = False
_tum_poll_lock = threading.Lock()
_tum_first_poll_done: set[int] = set()

# Persistent client — reused across poll cycles
_tum_client: TumClient | None = None


def _cleanup_tum_client():
    if _tum_client is not None:
        import asyncio
        try:
            asyncio.get_event_loop().run_until_complete(_tum_client.close())
        except Exception:
            logger.debug("Error alert send failed", exc_info=True)


atexit.register(_cleanup_tum_client)


def _update_tum_progress(phase: str, current: int = 0, total: int = 0, message: str = ""):
    tum_poll_progress["active"] = phase not in ("idle", "complete", "error")
    tum_poll_progress["phase"] = phase
    tum_poll_progress["current"] = current
    tum_poll_progress["total"] = total
    tum_poll_progress["message"] = message


def _send_tum_notifications(new_details: list[dict]) -> None:
    """Send Windows toast notifications for Tumblr activity."""
    settings = config.get_settings()
    n = len(new_details)
    notifications.maybe_show_toast(
        settings,
        "tum_notifications_enabled",
        f"TUM: {n} Post{'s' if n != 1 else ''} Updated",
        [f"{d['title'][:50]} gained notes" for d in new_details],
    )


async def _send_tum_telegram(new_details: list[dict]) -> None:
    """Send Telegram notification for Tumblr activity."""
    settings = config.get_settings()
    n = len(new_details)
    await notifications.maybe_send_telegram_summary(
        settings,
        f"<b>\U0001f4d8 TUM: {n} Post{'s' if n != 1 else ''} Updated</b>",
        [_esc(d['title'][:50]) for d in new_details],
        log_label="TUM",
    )


def _get_or_create_client(settings: dict, tum_api_key: str, tum_blog: str) -> TumClient:
    """Return the persistent TumClient, re-pointed at the account's credentials."""
    global _tum_client

    if _tum_client is None:
        from polling.cf_proxy import proxy_kwargs
        _tum_client = TumClient(
            api_key=tum_api_key,
            blog=tum_blog,
            **proxy_kwargs(settings, "tum"),
        )
    else:
        _tum_client.update_credentials(tum_api_key, tum_blog)

    return _tum_client


async def run_tum_poll_cycle(account_id: int | None = None, force_full: bool = False) -> dict:
    """Execute one complete Tumblr poll cycle for a single account."""
    global _tum_poll_running

    from database import accounts as accounts_db
    _ac = get_connection()
    try:
        if account_id is None:
            account_id = accounts_db.get_default_account_id(_ac, "tum", create=True)
        account_row = accounts_db.get_account(_ac, account_id)
    finally:
        _ac.close()
    is_default = bool(account_row["is_default"]) if account_row else True
    is_first = account_id not in _tum_first_poll_done

    if not _tum_poll_lock.acquire(blocking=False):
        logger.warning("TUM poll already running -- skipping (account %s)", account_id)
        return {}
    _tum_poll_running = True
    _update_tum_progress("starting", message="Initialising TUM poll cycle...")

    conn = None
    log_id = None
    start_time = time.time()

    stats = {
        "submissions_found": 0,
        "snapshots_inserted": 0,
    }

    settings = config.get_settings()
    creds = config.resolve_account_credentials("tum", account_id, is_default, settings)
    client = _get_or_create_client(settings, creds.get("tum_api_key", ""),
                                   creds.get("tum_blog", ""))

    try:
        conn = get_connection()
        log_id = tum_queries.start_tum_poll_log(conn, account_id)

        # Step 1: Validate API key + blog
        _update_tum_progress("searching", message="Verifying Tumblr blog...")
        name = await client.validate_session()
        if not name:
            raise ValueError("Tumblr lookup failed -- check API key and blog identifier")

        # Step 2: Discover posts
        _update_tum_progress("searching", message="Fetching post list...")
        post_items = await client.get_all_post_uris()
        stats["submissions_found"] = len(post_items)
        logger.info("TUM: Found %d posts", len(post_items))

        if not post_items:
            _update_tum_progress("complete", message="No Tumblr posts found.")
            tum_queries.finish_tum_poll_log(conn, log_id, "success",
                                            duration_seconds=time.time() - start_time, **stats)
            conn.commit()
            return stats

        # Step 3: Parse details (no extra round-trip)
        _update_tum_progress("fetching_details",
                            message=f"Parsing details for {len(post_items)} posts...")
        details = await client.get_post_details_batch(post_items)
        logger.info("TUM: Parsed details for %d posts", len(details))

        # Step 4: Upsert + snapshot
        new_activity_details: list[dict] = []
        poll_timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

        for idx, detail in enumerate(details, 1):
            _update_tum_progress("processing", current=idx, total=len(details),
                                message=f"Processing post {idx}/{len(details)}...")
            try:
                uri = detail["post_uri"]
                notes = detail.get("notes", 0)

                prev = tum_queries.get_tum_submission(conn, uri)
                if prev and notes > prev.get("notes", 0):
                    new_activity_details.append({"title": detail.get("title", "")})

                tum_queries.upsert_tum_submission(conn, detail, account_id)
                tum_queries.insert_tum_snapshot(conn, account_id, uri, notes, polled_at=poll_timestamp)
                stats["snapshots_inserted"] += 1

            except Exception as e:
                logger.warning("Error processing TUM post %s: %s",
                               detail.get("post_uri", "")[:50], e, exc_info=True)

        conn.commit()

        # ── Notifications ─────────────────────────────────────
        if is_first:
            logger.info("First TUM poll for account %s -- suppressing %d activity notifications",
                        account_id, len(new_activity_details))
        else:
            try:
                _send_tum_notifications(new_activity_details)
            except Exception as ne:
                logger.warning("Failed to send TUM notifications: %s", ne, exc_info=True)
            try:
                await _send_tum_telegram(new_activity_details)
            except Exception as te:
                logger.warning("Failed to send TUM Telegram notification: %s", te, exc_info=True)

        # Finalise
        duration = time.time() - start_time
        _update_tum_progress("complete", current=len(details), total=len(details),
                            message=f"Done -- {stats['submissions_found']} posts in {duration:.1f}s")
        tum_queries.finish_tum_poll_log(conn, log_id, "success",
                                        duration_seconds=duration, **stats)
        logger.info("TUM poll complete in %.1fs -- %d posts, %d snapshots",
                     duration, stats["submissions_found"], stats["snapshots_inserted"])

        # -- Telegram notifications ----------------------------------------
        if not is_first:
            from polling.telegram import send_poll_summary, check_milestones_batch, check_goals
            try:
                await send_poll_summary("tum", stats, duration)
            except Exception as te:
                logger.warning("Failed to send TUM Telegram summary: %s", te, exc_info=True)
            try:
                await check_milestones_batch("tum", "tum_snapshots", "tum_submissions", account_id)
            except Exception as me:
                logger.warning("Failed to check TUM milestones: %s", me, exc_info=True)
            try:
                await check_goals()
            except Exception as ge:
                logger.warning("Failed to check goals: %s", ge, exc_info=True)

        return stats

    except Exception as e:
        duration = time.time() - start_time
        _update_tum_progress("error", message=describe_error(e))
        logger.error("TUM poll failed: %s", describe_error(e), exc_info=True)
        if conn and log_id:
            tum_queries.finish_tum_poll_log(conn, log_id, "error",
                                            error_message=describe_error(e),
                                            duration_seconds=duration, **stats)
            conn.commit()
        from polling.telegram import send_poll_error
        try:
            await send_poll_error("tum", e)
        except Exception:
            logger.debug("Error alert send failed", exc_info=True)
        raise
    finally:
        _tum_first_poll_done.add(account_id)
        _tum_poll_running = False
        _tum_poll_lock.release()
        if conn:
            conn.close()
