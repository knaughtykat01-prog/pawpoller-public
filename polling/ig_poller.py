"""Instagram (IG) poll cycle orchestration.

Official Instagram Graph API (graph.instagram.com), OAuth long-lived token.

Key differences from other pollers:
  - Uses IgClient with access_token + optional target user_id
  - Settings keys: ig_access_token, ig_user_id
  - Metrics: views, reach, likes, comments, saved, shares
  - Notifications: "IG:" prefix, camera emoji
"""

from __future__ import annotations
import atexit
import logging
import threading
import time
from datetime import datetime, timezone
from html import escape as _esc

import config
from clients.ig.client import IgClient
from database.db import get_connection
from polling.notifications import describe_error
from database import ig_queries
from polling import notifications

logger = logging.getLogger(__name__)

# -- Progress tracking --------------------------------------------------------
ig_poll_progress = {
    "active": False,
    "phase": "idle",
    "current": 0,
    "total": 0,
    "message": "",
}

_ig_poll_running = False
_ig_poll_lock = threading.Lock()
_ig_first_poll_done: set[int] = set()

# Persistent client — reused across poll cycles
_ig_client: IgClient | None = None


def _cleanup_ig_client():
    if _ig_client is not None:
        import asyncio
        try:
            asyncio.get_event_loop().run_until_complete(_ig_client.close())
        except Exception:
            logger.debug("Error alert send failed", exc_info=True)


atexit.register(_cleanup_ig_client)


def _update_ig_progress(phase: str, current: int = 0, total: int = 0, message: str = ""):
    ig_poll_progress["active"] = phase not in ("idle", "complete", "error")
    ig_poll_progress["phase"] = phase
    ig_poll_progress["current"] = current
    ig_poll_progress["total"] = total
    ig_poll_progress["message"] = message


def _send_ig_notifications(new_details: list[dict]) -> None:
    """Send Windows toast notifications for Instagram activity."""
    settings = config.get_settings()
    n = len(new_details)
    notifications.maybe_show_toast(
        settings,
        "ig_notifications_enabled",
        f"IG: {n} Post{'s' if n != 1 else ''} Updated",
        [f"{d['title'][:50]} gained activity" for d in new_details],
    )


async def _send_ig_telegram(new_details: list[dict]) -> None:
    """Send Telegram notification for Instagram activity."""
    settings = config.get_settings()
    n = len(new_details)
    await notifications.maybe_send_telegram_summary(
        settings,
        f"<b>\U0001f4f8 IG: {n} Post{'s' if n != 1 else ''} Updated</b>",
        [_esc(d['title'][:50]) for d in new_details],
        log_label="IG",
    )


def _get_or_create_client(settings: dict, ig_access_token: str, ig_user_id: str) -> IgClient:
    """Return the persistent IgClient, re-pointed at the account's credentials."""
    global _ig_client

    if _ig_client is None:
        from polling.cf_proxy import proxy_kwargs
        _ig_client = IgClient(
            access_token=ig_access_token,
            user_id=ig_user_id,
            **proxy_kwargs(settings, "ig"),
        )
    else:
        _ig_client.update_credentials(ig_access_token, ig_user_id)

    return _ig_client


async def run_ig_poll_cycle(account_id: int | None = None, force_full: bool = False) -> dict:
    """Execute one complete Instagram poll cycle for a single account."""
    global _ig_poll_running

    from database import accounts as accounts_db
    _ac = get_connection()
    try:
        if account_id is None:
            account_id = accounts_db.get_default_account_id(_ac, "ig", create=True)
        account_row = accounts_db.get_account(_ac, account_id)
    finally:
        _ac.close()
    is_default = bool(account_row["is_default"]) if account_row else True
    is_first = account_id not in _ig_first_poll_done

    if not _ig_poll_lock.acquire(blocking=False):
        logger.warning("IG poll already running -- skipping (account %s)", account_id)
        return {}
    _ig_poll_running = True
    _update_ig_progress("starting", message="Initialising IG poll cycle...")

    conn = None
    log_id = None
    start_time = time.time()

    stats = {
        "submissions_found": 0,
        "snapshots_inserted": 0,
    }

    settings = config.get_settings()
    creds = config.resolve_account_credentials("ig", account_id, is_default, settings)
    client = _get_or_create_client(settings, creds.get("ig_access_token", ""),
                                   creds.get("ig_user_id", ""))

    try:
        conn = get_connection()
        log_id = ig_queries.start_ig_poll_log(conn, account_id)

        # Step 1: Validate token / resolve account
        _update_ig_progress("searching", message="Authenticating with Instagram...")
        name = await client.validate_session()
        if not name:
            raise ValueError("Instagram auth failed -- check the access token")

        # Step 2: Discover posts
        _update_ig_progress("searching", message="Fetching media list...")
        post_items = await client.get_all_post_uris()
        stats["submissions_found"] = len(post_items)
        logger.info("IG: Found %d media", len(post_items))

        if not post_items:
            _update_ig_progress("complete", message="No Instagram posts found.")
            ig_queries.finish_ig_poll_log(conn, log_id, "success",
                                          duration_seconds=time.time() - start_time, **stats)
            conn.commit()
            return stats

        # Step 3: Fetch per-post insights
        _update_ig_progress("fetching_details",
                          message=f"Fetching insights for {len(post_items)} posts...")
        details = await client.get_post_details_batch(post_items)
        logger.info("IG: Fetched insights for %d posts", len(details))

        # Step 4: Upsert + snapshot
        new_activity_details: list[dict] = []
        poll_timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

        for idx, detail in enumerate(details, 1):
            _update_ig_progress("processing", current=idx, total=len(details),
                              message=f"Processing post {idx}/{len(details)}...")
            try:
                uri = detail["post_uri"]
                views = detail.get("views", 0)
                reach = detail.get("reach", 0)
                likes = detail.get("likes", 0)
                comments = detail.get("comments", 0)
                saved = detail.get("saved", 0)
                shares = detail.get("shares", 0)

                prev = ig_queries.get_ig_submission(conn, uri)
                if prev and (likes > prev.get("likes", 0)
                             or comments > prev.get("comments", 0)):
                    new_activity_details.append({"title": detail.get("title", "")})

                ig_queries.upsert_ig_submission(conn, detail, account_id)
                ig_queries.insert_ig_snapshot(conn, account_id, uri, views, reach, likes,
                                              comments, saved, shares, polled_at=poll_timestamp)
                stats["snapshots_inserted"] += 1

            except Exception as e:
                logger.warning("Error processing IG post %s: %s",
                               detail.get("post_uri", "")[:50], e, exc_info=True)

        conn.commit()

        # ── Notifications ─────────────────────────────────────
        if is_first:
            logger.info("First IG poll for account %s -- suppressing %d activity notifications",
                        account_id, len(new_activity_details))
        else:
            try:
                _send_ig_notifications(new_activity_details)
            except Exception as ne:
                logger.warning("Failed to send IG notifications: %s", ne, exc_info=True)
            try:
                await _send_ig_telegram(new_activity_details)
            except Exception as te:
                logger.warning("Failed to send IG Telegram notification: %s", te, exc_info=True)

        # Finalise
        duration = time.time() - start_time
        _update_ig_progress("complete", current=len(details), total=len(details),
                          message=f"Done -- {stats['submissions_found']} posts in {duration:.1f}s")
        ig_queries.finish_ig_poll_log(conn, log_id, "success",
                                      duration_seconds=duration, **stats)
        logger.info("IG poll complete in %.1fs -- %d posts, %d snapshots",
                     duration, stats["submissions_found"], stats["snapshots_inserted"])

        # -- Telegram notifications ----------------------------------------
        if not is_first:
            from polling.telegram import send_poll_summary, check_milestones_batch, check_goals
            try:
                await send_poll_summary("ig", stats, duration)
            except Exception as te:
                logger.warning("Failed to send IG Telegram summary: %s", te, exc_info=True)
            try:
                await check_milestones_batch("ig", "ig_snapshots", "ig_submissions", account_id)
            except Exception as me:
                logger.warning("Failed to check IG milestones: %s", me, exc_info=True)
            try:
                await check_goals()
            except Exception as ge:
                logger.warning("Failed to check goals: %s", ge, exc_info=True)

        return stats

    except Exception as e:
        duration = time.time() - start_time
        _update_ig_progress("error", message=describe_error(e))
        logger.error("IG poll failed: %s", describe_error(e), exc_info=True)
        if conn and log_id:
            ig_queries.finish_ig_poll_log(conn, log_id, "error",
                                          error_message=describe_error(e),
                                          duration_seconds=duration, **stats)
            conn.commit()
        from polling.telegram import send_poll_error
        try:
            await send_poll_error("ig", e)
        except Exception:
            logger.debug("Error alert send failed", exc_info=True)
        raise
    finally:
        _ig_first_poll_done.add(account_id)
        _ig_poll_running = False
        _ig_poll_lock.release()
        if conn:
            conn.close()
