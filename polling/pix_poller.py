"""Pixiv (PIX) poll cycle orchestration.

Reverse-engineered app-API (pixivpy-style), OAuth via a refresh token.

Key differences from other pollers:
  - Uses PixClient with refresh_token + optional target user_id
  - Settings keys: pix_refresh_token, pix_user_id
  - Gallery metrics: views, favorites_count (bookmarks), comments_count
  - Notifications: "PIX:" prefix, paintbrush emoji
"""

from __future__ import annotations
import atexit
import logging
import threading
import time
from datetime import datetime, timezone
from html import escape as _esc

import config
from clients.pix.client import PixClient
from database.db import get_connection
from polling.notifications import describe_error
from polling.followers import capture_followers
from database import pix_queries
from polling import notifications

logger = logging.getLogger(__name__)

# -- Progress tracking --------------------------------------------------------
pix_poll_progress = {
    "active": False,
    "phase": "idle",
    "current": 0,
    "total": 0,
    "message": "",
}

_pix_poll_running = False
_pix_poll_lock = threading.Lock()
_pix_first_poll_done: set[int] = set()

# Persistent client — reused across poll cycles
_pix_client: PixClient | None = None


def _cleanup_pix_client():
    if _pix_client is not None:
        import asyncio
        try:
            asyncio.get_event_loop().run_until_complete(_pix_client.close())
        except Exception:
            logger.debug("Error alert send failed", exc_info=True)


atexit.register(_cleanup_pix_client)


def _update_pix_progress(phase: str, current: int = 0, total: int = 0, message: str = ""):
    pix_poll_progress["active"] = phase not in ("idle", "complete", "error")
    pix_poll_progress["phase"] = phase
    pix_poll_progress["current"] = current
    pix_poll_progress["total"] = total
    pix_poll_progress["message"] = message


def _send_pix_notifications(new_details: list[dict]) -> None:
    """Send Windows toast notifications for Pixiv activity."""
    settings = config.get_settings()
    n = len(new_details)
    notifications.maybe_show_toast(
        settings,
        "pix_notifications_enabled",
        f"PIX: {n} Work{'s' if n != 1 else ''} Updated",
        [f"{d['title'][:50]} gained activity" for d in new_details],
    )


async def _send_pix_telegram(new_details: list[dict]) -> None:
    """Send Telegram notification for Pixiv activity."""
    settings = config.get_settings()
    n = len(new_details)
    await notifications.maybe_send_telegram_summary(
        settings,
        f"<b>\U0001f58c PIX: {n} Work{'s' if n != 1 else ''} Updated</b>",
        [_esc(d['title'][:50]) for d in new_details],
        log_label="PIX",
    )


def _get_or_create_client(settings: dict, pix_refresh_token: str, pix_user_id: str) -> PixClient:
    """Return the persistent PixClient, re-pointed at the account's credentials."""
    global _pix_client

    if _pix_client is None:
        from polling.cf_proxy import proxy_kwargs
        _pix_client = PixClient(
            refresh_token=pix_refresh_token,
            user_id=pix_user_id,
            **proxy_kwargs(settings, "pix"),
        )
    else:
        _pix_client.update_credentials(pix_refresh_token, pix_user_id)

    return _pix_client


async def run_pix_poll_cycle(account_id: int | None = None, force_full: bool = False) -> dict:
    """Execute one complete Pixiv poll cycle for a single account."""
    global _pix_poll_running

    from database import accounts as accounts_db
    _ac = get_connection()
    try:
        if account_id is None:
            account_id = accounts_db.get_default_account_id(_ac, "pix", create=True)
        account_row = accounts_db.get_account(_ac, account_id)
    finally:
        _ac.close()
    is_default = bool(account_row["is_default"]) if account_row else True
    is_first = account_id not in _pix_first_poll_done

    if not _pix_poll_lock.acquire(blocking=False):
        logger.warning("PIX poll already running -- skipping (account %s)", account_id)
        return {}
    _pix_poll_running = True
    _update_pix_progress("starting", message="Initialising PIX poll cycle...")

    conn = None
    log_id = None
    start_time = time.time()

    stats = {
        "submissions_found": 0,
        "snapshots_inserted": 0,
    }

    settings = config.get_settings()
    creds = config.resolve_account_credentials("pix", account_id, is_default, settings)
    client = _get_or_create_client(settings, creds.get("pix_refresh_token", ""),
                                   creds.get("pix_user_id", ""))

    try:
        conn = get_connection()
        log_id = pix_queries.start_pix_poll_log(conn, account_id)

        # Step 1: Refresh OAuth token
        _update_pix_progress("searching", message="Authenticating with Pixiv...")
        name = await client.validate_session()
        if not name:
            raise ValueError("Pixiv auth failed -- check the refresh token")

        # Step 2: Discover works
        _update_pix_progress("searching", message="Fetching work list...")
        post_items = await client.get_all_post_uris()
        stats["submissions_found"] = len(post_items)
        logger.info("PIX: Found %d works", len(post_items))

        if not post_items:
            _update_pix_progress("complete", message="No Pixiv works found.")
            pix_queries.finish_pix_poll_log(conn, log_id, "success",
                                            duration_seconds=time.time() - start_time, **stats)
            conn.commit()
            return stats

        # Step 3: Parse details (no extra round-trip)
        _update_pix_progress("fetching_details",
                            message=f"Parsing details for {len(post_items)} works...")
        details = await client.get_post_details_batch(post_items)
        logger.info("PIX: Parsed details for %d works", len(details))

        # Step 4: Upsert + snapshot
        new_activity_details: list[dict] = []
        poll_timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

        for idx, detail in enumerate(details, 1):
            _update_pix_progress("processing", current=idx, total=len(details),
                                message=f"Processing work {idx}/{len(details)}...")
            try:
                uri = detail["post_uri"]
                views = detail.get("views", 0)
                faves = detail.get("favorites_count", 0)
                comments = detail.get("comments_count", 0)

                prev = pix_queries.get_pix_submission(conn, uri)
                if prev and (faves > prev.get("favorites_count", 0)
                             or comments > prev.get("comments_count", 0)):
                    new_activity_details.append({"title": detail.get("title", "")})

                pix_queries.upsert_pix_submission(conn, detail, account_id)
                pix_queries.insert_pix_snapshot(conn, account_id, uri, views, faves,
                                                comments, polled_at=poll_timestamp)
                stats["snapshots_inserted"] += 1

            except Exception as e:
                logger.warning("Error processing PIX work %s: %s",
                               detail.get("post_uri", "")[:50], e, exc_info=True)

        conn.commit()

        # ── Notifications ─────────────────────────────────────
        if is_first:
            logger.info("First PIX poll for account %s -- suppressing %d activity notifications",
                        account_id, len(new_activity_details))
        else:
            try:
                _send_pix_notifications(new_activity_details)
            except Exception as ne:
                logger.warning("Failed to send PIX notifications: %s", ne, exc_info=True)
            try:
                await _send_pix_telegram(new_activity_details)
            except Exception as te:
                logger.warning("Failed to send PIX Telegram notification: %s", te, exc_info=True)

        # Finalise
        # Follower count: reuse the authed client to snapshot the account's
        # follower total (network fetch first, then a short DB write — no lock
        # held across the await). Best-effort; never fails the cycle.
        await capture_followers(client, account_id, conn)

        duration = time.time() - start_time
        _update_pix_progress("complete", current=len(details), total=len(details),
                            message=f"Done -- {stats['submissions_found']} works in {duration:.1f}s")
        pix_queries.finish_pix_poll_log(conn, log_id, "success",
                                        duration_seconds=duration, **stats)
        logger.info("PIX poll complete in %.1fs -- %d works, %d snapshots",
                     duration, stats["submissions_found"], stats["snapshots_inserted"])

        # -- Telegram notifications ----------------------------------------
        if not is_first:
            from polling.telegram import send_poll_summary, check_milestones_batch, check_goals
            try:
                await send_poll_summary("pix", stats, duration)
            except Exception as te:
                logger.warning("Failed to send PIX Telegram summary: %s", te, exc_info=True)
            try:
                await check_milestones_batch("pix", "pix_snapshots", "pix_submissions", account_id)
            except Exception as me:
                logger.warning("Failed to check PIX milestones: %s", me, exc_info=True)
            try:
                await check_goals()
            except Exception as ge:
                logger.warning("Failed to check goals: %s", ge, exc_info=True)

        return stats

    except Exception as e:
        duration = time.time() - start_time
        _update_pix_progress("error", message=describe_error(e))
        logger.error("PIX poll failed: %s", describe_error(e), exc_info=True)
        if conn and log_id:
            pix_queries.finish_pix_poll_log(conn, log_id, "error",
                                            error_message=describe_error(e),
                                            duration_seconds=duration, **stats)
            conn.commit()
        from polling.telegram import send_poll_error
        try:
            await send_poll_error("pix", e)
        except Exception:
            logger.debug("Error alert send failed", exc_info=True)
        raise
    finally:
        _pix_first_poll_done.add(account_id)
        _pix_poll_running = False
        _pix_poll_lock.release()
        if conn:
            conn.close()
