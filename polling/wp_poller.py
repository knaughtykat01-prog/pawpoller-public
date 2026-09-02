"""Wattpad (WP) poll cycle orchestration.

Simpler than other pollers since Wattpad's public API requires no
authentication — only a target username is needed. Stories are discovered
via the user profile endpoint and stats (reads, votes, comments,
reading lists) are fetched per-story.

Key differences from other platform pollers:
  - No login/authentication step
  - Only needs wp_target_user setting
  - No kudos/fave/comment individual user tracking — just counts
  - Unique metric: num_lists (reading lists)
"""

from __future__ import annotations
import atexit
import logging
import threading
import time
from datetime import datetime, timezone
from html import escape as _esc

import config
from clients.wp.client import WPClient
from database.db import get_connection
from polling.notifications import describe_error
from polling.followers import capture_followers
from database import wp_queries
from polling import notifications

logger = logging.getLogger(__name__)

# -- Progress tracking -----------------------------------------------------
wp_poll_progress = {
    "active": False,
    "phase": "idle",
    "current": 0,
    "total": 0,
    "message": "",
}

_wp_poll_running = False
_wp_poll_lock = threading.Lock()
_wp_first_poll_done: set[int] = set()

# Persistent client — reused across poll cycles
_wp_client: WPClient | None = None


def _cleanup_wp_client():
    if _wp_client is not None:
        import asyncio
        try:
            asyncio.get_event_loop().run_until_complete(_wp_client.close())
        except Exception:
            logger.debug("Error alert send failed", exc_info=True)


atexit.register(_cleanup_wp_client)


def _update_wp_progress(phase: str, current: int = 0, total: int = 0, message: str = ""):
    wp_poll_progress["active"] = phase not in ("idle", "complete", "error")
    wp_poll_progress["phase"] = phase
    wp_poll_progress["current"] = current
    wp_poll_progress["total"] = total
    wp_poll_progress["message"] = message


def _send_wp_notifications(new_details: list[dict]) -> None:
    """Send Windows toast notifications for Wattpad activity."""
    settings = config.get_settings()
    n = len(new_details)
    notifications.maybe_show_toast(
        settings,
        "wp_notifications_enabled",
        f"WP: {n} Stor{'ies' if n != 1 else 'y'} Updated",
        [f"{d['title']} gained activity" for d in new_details],
    )


async def _send_wp_telegram(new_details: list[dict]) -> None:
    """Send Telegram notification for Wattpad activity."""
    settings = config.get_settings()
    n = len(new_details)
    await notifications.maybe_send_telegram_summary(
        settings,
        f"<b>\U0001f4d9 WP: {n} Stor{'ies' if n != 1 else 'y'} Updated</b>",
        [_esc(d['title']) for d in new_details],
        log_label="WP",
    )


def _get_or_create_client(settings: dict, wp_target: str) -> WPClient:
    """Return the persistent WPClient, re-pointed at the account's target user."""
    global _wp_client

    if _wp_client is None:
        from polling.cf_proxy import proxy_kwargs
        _wp_client = WPClient(target_user=wp_target, **proxy_kwargs(settings, "wp"))
    else:
        _wp_client.update_credentials(wp_target)

    return _wp_client


async def run_wp_poll_cycle(account_id: int | None = None, force_full: bool = False) -> dict:
    """Execute one complete Wattpad poll cycle for a single account.

    Steps:
      1. Validate the target user exists
      2. Discover all stories for the target user
      3. Fetch details for each story
      4. Upsert stories and record snapshots
    """
    global _wp_poll_running

    from database import accounts as accounts_db
    _ac = get_connection()
    try:
        if account_id is None:
            account_id = accounts_db.get_default_account_id(_ac, "wp", create=True)
        account_row = accounts_db.get_account(_ac, account_id)
    finally:
        _ac.close()
    is_default = bool(account_row["is_default"]) if account_row else True
    is_first = account_id not in _wp_first_poll_done

    if not _wp_poll_lock.acquire(blocking=False):
        logger.warning("WP poll already running -- skipping (account %s)", account_id)
        return {}
    _wp_poll_running = True
    _update_wp_progress("starting", message="Initialising WP poll cycle...")

    conn = None
    log_id = None
    start_time = time.time()

    stats = {
        "submissions_found": 0,
        "snapshots_inserted": 0,
    }

    settings = config.get_settings()
    creds = config.resolve_account_credentials("wp", account_id, is_default, settings)
    client = _get_or_create_client(settings, creds.get("wp_target_user", ""))

    try:
        conn = get_connection()
        log_id = wp_queries.start_wp_poll_log(conn, account_id)
        # Step 1: Validate user
        _update_wp_progress("searching", message="Validating Wattpad user...")
        target = await client.validate_user()
        if not target:
            raise ValueError("Wattpad user not found -- check wp_target_user setting")

        # Step 2: Discover stories
        _update_wp_progress("searching", message="Fetching stories list...")
        stories = await client.get_all_story_ids()
        story_ids = [s["story_id"] for s in stories]
        stats["submissions_found"] = len(story_ids)
        logger.info("WP: Found %d stories", len(story_ids))

        if not story_ids:
            _update_wp_progress("complete", message="No Wattpad stories found.")
            wp_queries.finish_wp_poll_log(conn, log_id, "success",
                                          duration_seconds=time.time() - start_time, **stats)
            conn.commit()
            return stats

        # Step 3: Fetch details
        _update_wp_progress("fetching_details",
                            message=f"Fetching details for {len(story_ids)} stories...")
        details = await client.get_story_details_batch(story_ids)
        logger.info("WP: Fetched details for %d stories", len(details))

        # Step 4: Upsert + snapshot
        new_activity_details: list[dict] = []
        poll_timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

        for idx, detail in enumerate(details, 1):
            _update_wp_progress("processing", current=idx, total=len(details),
                                message=f"Processing story {idx}/{len(details)}...")
            try:
                sid = detail["story_id"]
                reads = detail.get("reads", 0)
                votes = detail.get("votes", 0)
                comments = detail.get("comments_count", 0)
                num_lists = detail.get("num_lists", 0)

                # Check for stat increases to drive notifications.
                prev = wp_queries.get_wp_submission(conn, sid)
                if prev and (votes > prev.get("votes", 0)
                             or comments > prev.get("comments_count", 0)):
                    new_activity_details.append({"title": detail.get("title", "")})

                wp_queries.upsert_wp_submission(conn, detail, account_id)
                wp_queries.insert_wp_snapshot(conn, account_id, sid, reads, votes, comments,
                                              num_lists, polled_at=poll_timestamp)
                stats["snapshots_inserted"] += 1

            except Exception as e:
                logger.warning("Error processing WP story %s: %s",
                               detail.get("story_id"), e, exc_info=True)

        conn.commit()

        # ── Notifications ─────────────────────────────────────
        if is_first:
            logger.info("First WP poll for account %s -- suppressing %d activity notifications",
                        account_id, len(new_activity_details))
        else:
            try:
                _send_wp_notifications(new_activity_details)
            except Exception as ne:
                logger.warning("Failed to send WP notifications: %s", ne, exc_info=True)
            try:
                await _send_wp_telegram(new_activity_details)
            except Exception as te:
                logger.warning("Failed to send WP Telegram notification: %s", te, exc_info=True)

        # Finalise
        # Follower count: reuse the authed client to snapshot the account's
        # follower total (network fetch first, then a short DB write — no lock
        # held across the await). Best-effort; never fails the cycle.
        await capture_followers(client, account_id, conn)

        duration = time.time() - start_time
        _update_wp_progress("complete", current=len(details), total=len(details),
                            message=f"Done -- {stats['submissions_found']} stories in {duration:.1f}s")
        wp_queries.finish_wp_poll_log(conn, log_id, "success",
                                      duration_seconds=duration, **stats)
        logger.info("WP poll complete in %.1fs -- %d stories, %d snapshots",
                     duration, stats["submissions_found"], stats["snapshots_inserted"])

        # -- Telegram notifications ----------------------------------------
        if not is_first:
            from polling.telegram import send_poll_summary, check_milestones_batch, check_goals
            try:
                await send_poll_summary("wp", stats, duration)
            except Exception as te:
                logger.warning("Failed to send WP Telegram summary: %s", te, exc_info=True)
            try:
                await check_milestones_batch("wp", "wp_snapshots", "wp_submissions", account_id)
            except Exception as me:
                logger.warning("Failed to check WP milestones: %s", me, exc_info=True)
            try:
                await check_goals()
            except Exception as ge:
                logger.warning("Failed to check goals: %s", ge, exc_info=True)

        return stats

    except Exception as e:
        duration = time.time() - start_time
        _update_wp_progress("error", message=describe_error(e))
        logger.error("WP poll failed: %s", describe_error(e), exc_info=True)
        if conn and log_id:
            wp_queries.finish_wp_poll_log(conn, log_id, "error",
                                          error_message=describe_error(e),
                                          duration_seconds=duration, **stats)
            conn.commit()
        from polling.telegram import send_poll_error
        try:
            await send_poll_error("wp", e)
        except Exception:
            logger.debug("Error alert send failed", exc_info=True)
        raise
    finally:
        _wp_first_poll_done.add(account_id)
        _wp_poll_running = False
        _wp_poll_lock.release()
        if conn:
            conn.close()
