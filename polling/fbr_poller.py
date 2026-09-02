"""fbr (Furbooru) poll cycle orchestration.

Official fbr REST API, HTTP Basic auth (username + API key). Poll-only.

Key differences from other pollers:
  - Uses FurbooruClient with username + api_key
  - Settings keys: fbr_username, fbr_api_key
  - Metrics: score (score.total, can be negative), favorites_count, comments_count
  - No follower series (fbr exposes no per-user follower count)
  - Notifications: "Furbooru:" prefix
"""

from __future__ import annotations
import atexit
import logging
import threading
import time
from datetime import datetime, timezone
from html import escape as _esc

import config
from clients.fbr.client import FurbooruClient
from database.db import get_connection
from polling.notifications import describe_error
from database import fbr_queries
from polling import notifications

logger = logging.getLogger(__name__)

# -- Progress tracking --------------------------------------------------------
fbr_poll_progress = {
    "active": False,
    "phase": "idle",
    "current": 0,
    "total": 0,
    "message": "",
}

_fbr_poll_running = False
_fbr_poll_lock = threading.Lock()
_fbr_first_poll_done: set[int] = set()

# Persistent client — reused across poll cycles
_fbr_client: FurbooruClient | None = None


def _cleanup_fbr_client():
    if _fbr_client is not None:
        import asyncio
        try:
            asyncio.get_event_loop().run_until_complete(_fbr_client.close())
        except Exception:
            logger.debug("fbr client cleanup failed", exc_info=True)


atexit.register(_cleanup_fbr_client)


def _update_fbr_progress(phase: str, current: int = 0, total: int = 0, message: str = ""):
    fbr_poll_progress["active"] = phase not in ("idle", "complete", "error")
    fbr_poll_progress["phase"] = phase
    fbr_poll_progress["current"] = current
    fbr_poll_progress["total"] = total
    fbr_poll_progress["message"] = message


def _send_fbr_notifications(new_details: list[dict]) -> None:
    """Send Windows toast notifications for fbr activity."""
    settings = config.get_settings()
    n = len(new_details)
    notifications.maybe_show_toast(
        settings,
        "fbr_notifications_enabled",
        f"Furbooru: {n} Post{'s' if n != 1 else ''} Updated",
        [f"{d['title'][:50]} gained activity" for d in new_details],
    )


async def _send_fbr_telegram(new_details: list[dict]) -> None:
    """Send Telegram notification for fbr activity."""
    settings = config.get_settings()
    n = len(new_details)
    await notifications.maybe_send_telegram_summary(
        settings,
        f"<b>\U0001f43e Furbooru: {n} Post{'s' if n != 1 else ''} Updated</b>",
        [_esc(d['title'][:50]) for d in new_details],
        log_label="Furbooru",
    )


def _get_or_create_client(settings: dict, fbr_username: str, fbr_api_key: str) -> FurbooruClient:
    """Return the persistent FurbooruClient, re-pointed at the account's credentials."""
    global _fbr_client

    if _fbr_client is None:
        _fbr_client = FurbooruClient(username=fbr_username, api_key=fbr_api_key)
    else:
        _fbr_client.update_credentials(fbr_username, fbr_api_key)

    return _fbr_client


async def run_fbr_poll_cycle(account_id: int | None = None, force_full: bool = False) -> dict:
    """Execute one complete fbr poll cycle for a single account."""
    global _fbr_poll_running

    from database import accounts as accounts_db
    _ac = get_connection()
    try:
        if account_id is None:
            account_id = accounts_db.get_default_account_id(_ac, "fbr", create=True)
        account_row = accounts_db.get_account(_ac, account_id)
    finally:
        _ac.close()
    is_default = bool(account_row["is_default"]) if account_row else True
    is_first = account_id not in _fbr_first_poll_done

    if not _fbr_poll_lock.acquire(blocking=False):
        logger.warning("fbr poll already running -- skipping (account %s)", account_id)
        return {}
    _fbr_poll_running = True
    _update_fbr_progress("starting", message="Initialising fbr poll cycle...")

    conn = None
    log_id = None
    start_time = time.time()

    stats = {
        "submissions_found": 0,
        "snapshots_inserted": 0,
    }

    settings = config.get_settings()
    creds = config.resolve_account_credentials("fbr", account_id, is_default, settings)
    client = _get_or_create_client(settings, creds.get("fbr_username", ""),
                                   creds.get("fbr_api_key", ""))

    try:
        conn = get_connection()
        log_id = fbr_queries.start_fbr_poll_log(conn, account_id)

        # Step 1: Verify credentials
        _update_fbr_progress("searching", message="Authenticating with fbr...")
        name = await client.validate_session()
        if not name:
            raise ValueError("fbr auth failed -- check the username + API key")

        # Step 2: Discover uploads
        _update_fbr_progress("searching", message="Fetching upload list...")
        post_items = await client.get_all_post_uris()
        stats["submissions_found"] = len(post_items)
        logger.info("fbr: found %d posts", len(post_items))

        if not post_items:
            _update_fbr_progress("complete", message="No fbr uploads found.")
            fbr_queries.finish_fbr_poll_log(conn, log_id, "success",
                                              duration_seconds=time.time() - start_time, **stats)
            conn.commit()
            return stats

        # Step 3: Parse details (no extra round-trip)
        _update_fbr_progress("fetching_details",
                              message=f"Parsing details for {len(post_items)} posts...")
        details = await client.get_post_details_batch(post_items)
        logger.info("fbr: parsed details for %d posts", len(details))

        # Step 4: Upsert + snapshot
        new_activity_details: list[dict] = []
        poll_timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

        for idx, detail in enumerate(details, 1):
            _update_fbr_progress("processing", current=idx, total=len(details),
                                  message=f"Processing post {idx}/{len(details)}...")
            try:
                uri = detail["post_uri"]
                score = detail.get("score", 0)
                up_score = detail.get("up_score", 0)
                down_score = detail.get("down_score", 0)
                faves = detail.get("favorites_count", 0)
                comments = detail.get("comments_count", 0)

                prev = fbr_queries.get_fbr_submission(conn, uri)
                if prev and (faves > prev.get("favorites_count", 0)
                             or comments > prev.get("comments_count", 0)):
                    new_activity_details.append({"title": detail.get("title", "")})

                fbr_queries.upsert_fbr_submission(conn, detail, account_id)
                fbr_queries.insert_fbr_snapshot(conn, account_id, uri, score, faves,
                                                  comments, polled_at=poll_timestamp,
                                                  up_score=up_score, down_score=down_score)
                stats["snapshots_inserted"] += 1

            except Exception as e:
                logger.warning("Error processing fbr post %s: %s",
                               detail.get("post_uri", "")[:50], e, exc_info=True)

        conn.commit()
        # (No inbox capture — the Philomena read API has no per-image comments
        # fetch wired here; Furbooru is poll-only for engagement counts.)

        # ── Notifications ─────────────────────────────────────
        if is_first:
            logger.info("First fbr poll for account %s -- suppressing %d activity notifications",
                        account_id, len(new_activity_details))
        else:
            try:
                _send_fbr_notifications(new_activity_details)
            except Exception as ne:
                logger.warning("Failed to send fbr notifications: %s", ne, exc_info=True)
            try:
                await _send_fbr_telegram(new_activity_details)
            except Exception as te:
                logger.warning("Failed to send fbr Telegram notification: %s", te, exc_info=True)

        duration = time.time() - start_time
        _update_fbr_progress("complete", current=len(details), total=len(details),
                              message=f"Done -- {stats['submissions_found']} posts in {duration:.1f}s")
        fbr_queries.finish_fbr_poll_log(conn, log_id, "success",
                                          duration_seconds=duration, **stats)
        logger.info("fbr poll complete in %.1fs -- %d posts, %d snapshots",
                    duration, stats["submissions_found"], stats["snapshots_inserted"])

        # -- Telegram notifications ----------------------------------------
        if not is_first:
            from polling.telegram import send_poll_summary, check_milestones_batch, check_goals
            try:
                await send_poll_summary("fbr", stats, duration)
            except Exception as te:
                logger.warning("Failed to send fbr Telegram summary: %s", te, exc_info=True)
            try:
                await check_milestones_batch("fbr", "fbr_snapshots", "fbr_submissions", account_id)
            except Exception as me:
                logger.warning("Failed to check fbr milestones: %s", me, exc_info=True)
            try:
                await check_goals()
            except Exception as ge:
                logger.warning("Failed to check goals: %s", ge, exc_info=True)

        return stats

    except Exception as e:
        duration = time.time() - start_time
        _update_fbr_progress("error", message=describe_error(e))
        logger.error("fbr poll failed: %s", describe_error(e), exc_info=True)
        if conn and log_id:
            fbr_queries.finish_fbr_poll_log(conn, log_id, "error",
                                              error_message=describe_error(e),
                                              duration_seconds=duration, **stats)
            conn.commit()
        from polling.telegram import send_poll_error
        try:
            await send_poll_error("fbr", e)
        except Exception:
            logger.debug("Error alert send failed", exc_info=True)
        raise
    finally:
        _fbr_first_poll_done.add(account_id)
        _fbr_poll_running = False
        _fbr_poll_lock.release()
        if conn:
            conn.close()
