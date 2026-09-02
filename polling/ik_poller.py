"""Itaku (IK) poll cycle orchestration.

Simpler than other pollers since Itaku's public API requires no
authentication — only a target username is needed. Content is discovered
via the user profile endpoint and stats (likes, comments, reshares)
are fetched per-item.

Key differences from other platform pollers:
  - No login/authentication step
  - Only needs ik_target_user setting
  - No kudos/fave/comment individual user tracking — just counts
  - Unique metric: reshares (num_reshares)
  - NO views metric available on Itaku
  - Content types: images and posts
"""

from __future__ import annotations
import atexit
import logging
import threading
import time
from datetime import datetime, timezone
from html import escape as _esc

import config
from clients.ik.client import IKClient
from database.db import get_connection
from polling.notifications import describe_error
from polling.followers import capture_followers
from database import ik_queries
from polling import notifications

logger = logging.getLogger(__name__)

# -- Progress tracking -----------------------------------------------------
ik_poll_progress = {
    "active": False,
    "phase": "idle",
    "current": 0,
    "total": 0,
    "message": "",
}

_ik_poll_running = False
_ik_poll_lock = threading.Lock()
_ik_first_poll_done: set[int] = set()

# Persistent client — reused across poll cycles
_ik_client: IKClient | None = None


def _cleanup_ik_client():
    if _ik_client is not None:
        import asyncio
        try:
            asyncio.get_event_loop().run_until_complete(_ik_client.close())
        except Exception:
            logger.debug("Error alert send failed", exc_info=True)


atexit.register(_cleanup_ik_client)


def _update_ik_progress(phase: str, current: int = 0, total: int = 0, message: str = ""):
    ik_poll_progress["active"] = phase not in ("idle", "complete", "error")
    ik_poll_progress["phase"] = phase
    ik_poll_progress["current"] = current
    ik_poll_progress["total"] = total
    ik_poll_progress["message"] = message


def _send_ik_notifications(new_details: list[dict]) -> None:
    """Send Windows toast notifications for Itaku activity."""
    settings = config.get_settings()
    n = len(new_details)
    notifications.maybe_show_toast(
        settings,
        "ik_notifications_enabled",
        f"IK: {n} Item{'s' if n != 1 else ''} Updated",
        [f"{d['title']} gained activity" for d in new_details],
    )


async def _send_ik_telegram(new_details: list[dict]) -> None:
    """Send Telegram notification for Itaku activity."""
    settings = config.get_settings()
    n = len(new_details)
    await notifications.maybe_send_telegram_summary(
        settings,
        f"<b>\U0001f3af IK: {n} Item{'s' if n != 1 else ''} Updated</b>",
        [_esc(d['title']) for d in new_details],
        log_label="IK",
    )


def _get_or_create_client(settings: dict, ik_target: str) -> IKClient:
    """Return the persistent IKClient, re-pointed at the account's target user."""
    global _ik_client

    if _ik_client is None:
        from polling.cf_proxy import proxy_kwargs
        _ik_client = IKClient(target_user=ik_target, **proxy_kwargs(settings, "ik"))
    else:
        _ik_client.update_credentials(ik_target)

    return _ik_client


async def run_ik_poll_cycle(account_id: int | None = None, force_full: bool = False) -> dict:
    """Execute one complete Itaku poll cycle for a single account.

    Steps:
      1. Validate the target user exists
      2. Discover all content (images + posts) for the target user
      3. Fetch details for each content item
      4. Upsert items and record snapshots
    """
    global _ik_poll_running

    from database import accounts as accounts_db
    _ac = get_connection()
    try:
        if account_id is None:
            account_id = accounts_db.get_default_account_id(_ac, "ik", create=True)
        account_row = accounts_db.get_account(_ac, account_id)
    finally:
        _ac.close()
    is_default = bool(account_row["is_default"]) if account_row else True
    is_first = account_id not in _ik_first_poll_done

    if not _ik_poll_lock.acquire(blocking=False):
        logger.warning("IK poll already running -- skipping (account %s)", account_id)
        return {}
    _ik_poll_running = True
    _update_ik_progress("starting", message="Initialising IK poll cycle...")

    conn = None
    log_id = None
    start_time = time.time()

    stats = {
        "submissions_found": 0,
        "snapshots_inserted": 0,
    }

    settings = config.get_settings()
    creds = config.resolve_account_credentials("ik", account_id, is_default, settings)
    client = _get_or_create_client(settings, creds.get("ik_target_user", ""))

    try:
        conn = get_connection()
        log_id = ik_queries.start_ik_poll_log(conn, account_id)
        # Step 1: Validate user
        _update_ik_progress("searching", message="Validating Itaku user...")
        target = await client.validate_user()
        if not target:
            raise ValueError("Itaku user not found -- check ik_target_user setting")

        # Step 2: Discover content
        _update_ik_progress("searching", message="Fetching content list...")
        content_items = await client.get_all_content_ids()
        stats["submissions_found"] = len(content_items)
        logger.info("IK: Found %d content items", len(content_items))

        if not content_items:
            _update_ik_progress("complete", message="No Itaku content found.")
            ik_queries.finish_ik_poll_log(conn, log_id, "success",
                                          duration_seconds=time.time() - start_time, **stats)
            conn.commit()
            return stats

        # Step 3: Fetch details
        _update_ik_progress("fetching_details",
                            message=f"Fetching details for {len(content_items)} items...")
        details = await client.get_content_details_batch(content_items)
        logger.info("IK: Fetched details for %d items", len(details))

        # Step 4: Upsert + snapshot
        new_activity_details: list[dict] = []
        poll_timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

        for idx, detail in enumerate(details, 1):
            _update_ik_progress("processing", current=idx, total=len(details),
                                message=f"Processing item {idx}/{len(details)}...")
            try:
                sid = detail["content_id"]
                likes = detail.get("likes", 0)
                comments = detail.get("comments_count", 0)
                reshares = detail.get("reshares", 0)

                # Check for stat increases to drive notifications.
                prev = ik_queries.get_ik_submission(conn, sid)
                if prev and (likes > prev.get("likes", 0)
                             or comments > prev.get("comments_count", 0)):
                    new_activity_details.append({"title": detail.get("title", "")})

                ik_queries.upsert_ik_submission(conn, detail, account_id)
                ik_queries.insert_ik_snapshot(conn, account_id, sid, likes, comments,
                                              reshares, polled_at=poll_timestamp)
                stats["snapshots_inserted"] += 1

            except Exception as e:
                logger.warning("Error processing IK item %s: %s",
                               detail.get("content_id"), e, exc_info=True)

        conn.commit()

        # ── Notifications ─────────────────────────────────────
        if is_first:
            logger.info("First IK poll for account %s -- suppressing %d activity notifications",
                        account_id, len(new_activity_details))
        else:
            try:
                _send_ik_notifications(new_activity_details)
            except Exception as ne:
                logger.warning("Failed to send IK notifications: %s", ne, exc_info=True)
            try:
                await _send_ik_telegram(new_activity_details)
            except Exception as te:
                logger.warning("Failed to send IK Telegram notification: %s", te, exc_info=True)

        # Finalise
        # Follower count: reuse the authed client to snapshot the account's
        # follower total (network fetch first, then a short DB write — no lock
        # held across the await). Best-effort; never fails the cycle.
        await capture_followers(client, account_id, conn)

        duration = time.time() - start_time
        _update_ik_progress("complete", current=len(details), total=len(details),
                            message=f"Done -- {stats['submissions_found']} items in {duration:.1f}s")
        ik_queries.finish_ik_poll_log(conn, log_id, "success",
                                      duration_seconds=duration, **stats)
        logger.info("IK poll complete in %.1fs -- %d items, %d snapshots",
                     duration, stats["submissions_found"], stats["snapshots_inserted"])

        # -- Telegram notifications ----------------------------------------
        if not is_first:
            from polling.telegram import send_poll_summary, check_milestones_batch, check_goals
            try:
                await send_poll_summary("ik", stats, duration)
            except Exception as te:
                logger.warning("Failed to send IK Telegram summary: %s", te, exc_info=True)
            try:
                await check_milestones_batch("ik", "ik_snapshots", "ik_submissions", account_id)
            except Exception as me:
                logger.warning("Failed to check IK milestones: %s", me, exc_info=True)
            try:
                await check_goals()
            except Exception as ge:
                logger.warning("Failed to check goals: %s", ge, exc_info=True)

        return stats

    except Exception as e:
        duration = time.time() - start_time
        _update_ik_progress("error", message=describe_error(e))
        logger.error("IK poll failed: %s", describe_error(e), exc_info=True)
        if conn and log_id:
            ik_queries.finish_ik_poll_log(conn, log_id, "error",
                                          error_message=describe_error(e),
                                          duration_seconds=duration, **stats)
            conn.commit()
        from polling.telegram import send_poll_error
        try:
            await send_poll_error("ik", e)
        except Exception:
            logger.debug("Error alert send failed", exc_info=True)
        raise
    finally:
        _ik_first_poll_done.add(account_id)
        _ik_poll_running = False
        _ik_poll_lock.release()
        if conn:
            conn.close()
