"""Bluesky (BSKY) poll cycle orchestration.

Uses the AT Protocol public API with JWT session auth (app passwords).
Simpler than cookie-based platforms since Bluesky provides a proper API.

Key differences from other pollers:
  - Uses BskyClient with identifier (handle) + app_password
  - Settings keys: bsky_identifier, bsky_app_password
  - Stats: likes, reposts, replies, quotes (NO views metric)
  - Notifications: "BSKY:" prefix, butterfly emoji
"""

from __future__ import annotations
import atexit
import logging
import threading
import time
from datetime import datetime, timezone
from html import escape as _esc

import config
from clients.bsky.client import BskyClient
from database.db import get_connection
from polling.notifications import describe_error
from polling.followers import capture_followers
from database import bsky_queries
from polling import notifications

logger = logging.getLogger(__name__)

# -- Progress tracking --------------------------------------------------------
bsky_poll_progress = {
    "active": False,
    "phase": "idle",
    "current": 0,
    "total": 0,
    "message": "",
}

_bsky_poll_running = False
_bsky_poll_lock = threading.Lock()
_bsky_first_poll_done: set[int] = set()

# Persistent client — reused across poll cycles
_bsky_client: BskyClient | None = None


def _cleanup_bsky_client():
    if _bsky_client is not None:
        import asyncio
        try:
            asyncio.get_event_loop().run_until_complete(_bsky_client.close())
        except Exception:
            logger.debug("Error alert send failed", exc_info=True)


atexit.register(_cleanup_bsky_client)


def _update_bsky_progress(phase: str, current: int = 0, total: int = 0, message: str = ""):
    bsky_poll_progress["active"] = phase not in ("idle", "complete", "error")
    bsky_poll_progress["phase"] = phase
    bsky_poll_progress["current"] = current
    bsky_poll_progress["total"] = total
    bsky_poll_progress["message"] = message


def _send_bsky_notifications(new_details: list[dict]) -> None:
    """Send Windows toast notifications for Bluesky activity."""
    settings = config.get_settings()
    n = len(new_details)
    notifications.maybe_show_toast(
        settings,
        "bsky_notifications_enabled",
        f"BSKY: {n} Post{'s' if n != 1 else ''} Updated",
        [f"{d['title'][:50]} gained activity" for d in new_details],
    )


async def _send_bsky_telegram(new_details: list[dict]) -> None:
    """Send Telegram notification for Bluesky activity."""
    settings = config.get_settings()
    n = len(new_details)
    await notifications.maybe_send_telegram_summary(
        settings,
        f"<b>\U0001f98b BSKY: {n} Post{'s' if n != 1 else ''} Updated</b>",
        [_esc(d['title'][:50]) for d in new_details],
        log_label="BSKY",
    )


def _get_or_create_client(settings: dict, bsky_identifier: str, bsky_app_password: str) -> BskyClient:
    """Return the persistent BskyClient, re-pointed at the account's credentials."""
    global _bsky_client

    if _bsky_client is None:
        from polling.cf_proxy import proxy_kwargs
        _bsky_client = BskyClient(
            identifier=bsky_identifier,
            app_password=bsky_app_password,
            **proxy_kwargs(settings, "bsky"),
        )
    else:
        _bsky_client.update_credentials(bsky_identifier, bsky_app_password)

    return _bsky_client


async def run_bsky_poll_cycle(account_id: int | None = None, force_full: bool = False) -> dict:
    """Execute one complete Bluesky poll cycle for a single account.

    Steps:
      1. Login via AT Protocol createSession
      2. Discover all posts for the authenticated user
      3. Fetch details for each post (batched, 25 per request)
      4. Upsert posts and record snapshots
    """
    global _bsky_poll_running

    from database import accounts as accounts_db
    _ac = get_connection()
    try:
        if account_id is None:
            account_id = accounts_db.get_default_account_id(_ac, "bsky", create=True)
        account_row = accounts_db.get_account(_ac, account_id)
    finally:
        _ac.close()
    is_default = bool(account_row["is_default"]) if account_row else True
    is_first = account_id not in _bsky_first_poll_done

    if not _bsky_poll_lock.acquire(blocking=False):
        logger.warning("BSKY poll already running -- skipping (account %s)", account_id)
        return {}
    _bsky_poll_running = True
    _update_bsky_progress("starting", message="Initialising BSKY poll cycle...")

    conn = None
    log_id = None
    start_time = time.time()

    stats = {
        "submissions_found": 0,
        "snapshots_inserted": 0,
    }

    settings = config.get_settings()
    creds = config.resolve_account_credentials("bsky", account_id, is_default, settings)
    client = _get_or_create_client(settings, creds.get("bsky_identifier", ""),
                                   creds.get("bsky_app_password", ""))

    try:
        conn = get_connection()
        log_id = bsky_queries.start_bsky_poll_log(conn, account_id)

        # Step 1: Login
        _update_bsky_progress("searching", message="Logging in to Bluesky...")
        handle = await client.validate_session()
        if not handle:
            raise ValueError("Bluesky login failed -- check identifier and app password")

        # Step 2: Discover posts
        _update_bsky_progress("searching", message="Fetching post list...")
        post_items = await client.get_all_post_uris()
        stats["submissions_found"] = len(post_items)
        logger.info("BSKY: Found %d posts", len(post_items))

        if not post_items:
            _update_bsky_progress("complete", message="No Bluesky posts found.")
            bsky_queries.finish_bsky_poll_log(conn, log_id, "success",
                                              duration_seconds=time.time() - start_time, **stats)
            conn.commit()
            return stats

        # Step 3: Fetch details (batched)
        _update_bsky_progress("fetching_details",
                              message=f"Fetching details for {len(post_items)} posts...")
        details = await client.get_post_details_batch(post_items)
        logger.info("BSKY: Fetched details for %d posts", len(details))

        # Step 4: Upsert + snapshot
        new_activity_details: list[dict] = []
        poll_timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

        for idx, detail in enumerate(details, 1):
            _update_bsky_progress("processing", current=idx, total=len(details),
                                  message=f"Processing post {idx}/{len(details)}...")
            try:
                uri = detail["post_uri"]
                likes = detail.get("likes", 0)
                reposts = detail.get("reposts", 0)
                replies = detail.get("replies", 0)
                quotes = detail.get("quotes", 0)

                # Check for stat increases to drive notifications
                prev = bsky_queries.get_bsky_submission(conn, uri)
                if prev and (likes > prev.get("likes", 0)
                             or reposts > prev.get("reposts", 0)):
                    new_activity_details.append({"title": detail.get("title", "")})

                bsky_queries.upsert_bsky_submission(conn, detail, account_id)
                bsky_queries.insert_bsky_snapshot(conn, account_id, uri, likes, reposts,
                                                  replies, quotes, polled_at=poll_timestamp)
                stats["snapshots_inserted"] += 1

            except Exception as e:
                logger.warning("Error processing BSKY post %s: %s",
                               detail.get("post_uri", "")[:50], e, exc_info=True)

        conn.commit()

        # ── Inbox capture (gap G3 Stage A1) ───────────────────
        # Reply content for posts whose reply count exceeds what's captured.
        # After the commit (no write txn across an await); capped per cycle.
        try:
            from polling.inbox_capture import capture as _inbox_capture

            async def _fetch_thread(c):
                rows = await client.get_post_thread(c["submission_id"])
                for r in rows:
                    r["meta"] = {"cid": r.pop("cid", ""),
                                 "root_uri": r.pop("root_uri", ""),
                                 "root_cid": r.pop("root_cid", "")}
                return rows

            await _inbox_capture(
                conn, "bsky",
                [{"submission_id": d.get("post_uri", ""),
                  "fresh_count": d.get("replies", 0) or 0,
                  "title": d.get("title", "")} for d in details],
                _fetch_thread, account_id=account_id,
                own_author=getattr(client, "_handle", "") or "")
        except Exception as ce:  # noqa: BLE001 — capture never fails the poll
            logger.warning("BSKY inbox capture failed: %s", ce)

        # ── Notifications ─────────────────────────────────────
        if is_first:
            logger.info("First BSKY poll for account %s -- suppressing %d activity notifications",
                        account_id, len(new_activity_details))
        else:
            try:
                _send_bsky_notifications(new_activity_details)
            except Exception as ne:
                logger.warning("Failed to send BSKY notifications: %s", ne, exc_info=True)
            try:
                await _send_bsky_telegram(new_activity_details)
            except Exception as te:
                logger.warning("Failed to send BSKY Telegram notification: %s", te, exc_info=True)

        # Finalise
        # Follower count: reuse the authed client to snapshot the account's
        # follower total (network fetch first, then a short DB write — no lock
        # held across the await). Best-effort; never fails the cycle.
        await capture_followers(client, account_id, conn)

        duration = time.time() - start_time
        _update_bsky_progress("complete", current=len(details), total=len(details),
                              message=f"Done -- {stats['submissions_found']} posts in {duration:.1f}s")
        bsky_queries.finish_bsky_poll_log(conn, log_id, "success",
                                          duration_seconds=duration, **stats)
        logger.info("BSKY poll complete in %.1fs -- %d posts, %d snapshots",
                     duration, stats["submissions_found"], stats["snapshots_inserted"])

        # -- Telegram notifications ----------------------------------------
        if not is_first:
            from polling.telegram import send_poll_summary, check_milestones_batch, check_goals
            try:
                await send_poll_summary("bsky", stats, duration)
            except Exception as te:
                logger.warning("Failed to send BSKY Telegram summary: %s", te, exc_info=True)
            try:
                await check_milestones_batch("bsky", "bsky_snapshots", "bsky_submissions", account_id)
            except Exception as me:
                logger.warning("Failed to check BSKY milestones: %s", me, exc_info=True)
            try:
                await check_goals()
            except Exception as ge:
                logger.warning("Failed to check goals: %s", ge, exc_info=True)

        return stats

    except Exception as e:
        duration = time.time() - start_time
        _update_bsky_progress("error", message=describe_error(e))
        logger.error("BSKY poll failed: %s", describe_error(e), exc_info=True)
        if conn and log_id:
            bsky_queries.finish_bsky_poll_log(conn, log_id, "error",
                                              error_message=describe_error(e),
                                              duration_seconds=duration, **stats)
            conn.commit()
        from polling.telegram import send_poll_error
        try:
            await send_poll_error("bsky", e)
        except Exception:
            logger.debug("Error alert send failed", exc_info=True)
        raise
    finally:
        _bsky_first_poll_done.add(account_id)
        _bsky_poll_running = False
        _bsky_poll_lock.release()
        if conn:
            conn.close()
