"""Mastodon (MAST) poll cycle orchestration.

Uses each instance's open REST API with a personal access token (scope: read).
Simpler than cookie-based platforms since Mastodon provides a proper API and
the statuses timeline already carries the engagement counts (no per-post fetch).

Key differences from other pollers:
  - Uses MastClient with instance_url + access_token
  - Settings keys: mast_instance_url, mast_access_token
  - Stats: likes (favourites), reposts (reblogs), replies (NO quote metric)
  - Notifications: "MAST:" prefix, elephant emoji
"""

from __future__ import annotations
import atexit
import logging
import threading
import time
from datetime import datetime, timezone
from html import escape as _esc

import config
from clients.mast.client import MastClient
from database.db import get_connection
from polling.notifications import describe_error
from polling.followers import capture_followers
from database import mast_queries
from polling import notifications

logger = logging.getLogger(__name__)

# -- Progress tracking --------------------------------------------------------
mast_poll_progress = {
    "active": False,
    "phase": "idle",
    "current": 0,
    "total": 0,
    "message": "",
}

_mast_poll_running = False
_mast_poll_lock = threading.Lock()
_mast_first_poll_done: set[int] = set()

# Persistent client — reused across poll cycles
_mast_client: MastClient | None = None


def _cleanup_mast_client():
    if _mast_client is not None:
        import asyncio
        try:
            asyncio.get_event_loop().run_until_complete(_mast_client.close())
        except Exception:
            logger.debug("Error alert send failed", exc_info=True)


atexit.register(_cleanup_mast_client)


def _update_mast_progress(phase: str, current: int = 0, total: int = 0, message: str = ""):
    mast_poll_progress["active"] = phase not in ("idle", "complete", "error")
    mast_poll_progress["phase"] = phase
    mast_poll_progress["current"] = current
    mast_poll_progress["total"] = total
    mast_poll_progress["message"] = message


def _send_mast_notifications(new_details: list[dict]) -> None:
    """Send Windows toast notifications for Mastodon activity."""
    settings = config.get_settings()
    n = len(new_details)
    notifications.maybe_show_toast(
        settings,
        "mast_notifications_enabled",
        f"MAST: {n} Post{'s' if n != 1 else ''} Updated",
        [f"{d['title'][:50]} gained activity" for d in new_details],
    )


async def _send_mast_telegram(new_details: list[dict]) -> None:
    """Send Telegram notification for Mastodon activity."""
    settings = config.get_settings()
    n = len(new_details)
    await notifications.maybe_send_telegram_summary(
        settings,
        f"<b>\U0001f418 MAST: {n} Post{'s' if n != 1 else ''} Updated</b>",
        [_esc(d['title'][:50]) for d in new_details],
        log_label="MAST",
    )


def _get_or_create_client(settings: dict, mast_instance_url: str, mast_access_token: str) -> MastClient:
    """Return the persistent MastClient, re-pointed at the account's credentials."""
    global _mast_client

    if _mast_client is None:
        from polling.cf_proxy import proxy_kwargs
        _mast_client = MastClient(
            instance_url=mast_instance_url,
            access_token=mast_access_token,
            **proxy_kwargs(settings, "mast"),
        )
    else:
        _mast_client.update_credentials(mast_instance_url, mast_access_token)

    return _mast_client


async def run_mast_poll_cycle(account_id: int | None = None, force_full: bool = False) -> dict:
    """Execute one complete Mastodon poll cycle for a single account.

    Steps:
      1. Verify the access token (verify_credentials)
      2. Discover all statuses for the authenticated account
      3. Parse details (counts come with the timeline — no extra fetch)
      4. Upsert statuses and record snapshots
    """
    global _mast_poll_running

    from database import accounts as accounts_db
    _ac = get_connection()
    try:
        if account_id is None:
            account_id = accounts_db.get_default_account_id(_ac, "mast", create=True)
        account_row = accounts_db.get_account(_ac, account_id)
    finally:
        _ac.close()
    is_default = bool(account_row["is_default"]) if account_row else True
    is_first = account_id not in _mast_first_poll_done

    if not _mast_poll_lock.acquire(blocking=False):
        logger.warning("MAST poll already running -- skipping (account %s)", account_id)
        return {}
    _mast_poll_running = True
    _update_mast_progress("starting", message="Initialising MAST poll cycle...")

    conn = None
    log_id = None
    start_time = time.time()

    stats = {
        "submissions_found": 0,
        "snapshots_inserted": 0,
    }

    settings = config.get_settings()
    creds = config.resolve_account_credentials("mast", account_id, is_default, settings)
    client = _get_or_create_client(settings, creds.get("mast_instance_url", ""),
                                   creds.get("mast_access_token", ""))

    try:
        conn = get_connection()
        log_id = mast_queries.start_mast_poll_log(conn, account_id)

        # Step 1: Validate token
        _update_mast_progress("searching", message="Verifying Mastodon token...")
        handle = await client.validate_session()
        if not handle:
            raise ValueError("Mastodon login failed -- check instance URL and access token")

        # Step 2: Discover statuses
        _update_mast_progress("searching", message="Fetching status list...")
        post_items = await client.get_all_post_uris()
        stats["submissions_found"] = len(post_items)
        logger.info("MAST: Found %d statuses", len(post_items))

        if not post_items:
            _update_mast_progress("complete", message="No Mastodon statuses found.")
            mast_queries.finish_mast_poll_log(conn, log_id, "success",
                                              duration_seconds=time.time() - start_time, **stats)
            conn.commit()
            return stats

        # Step 3: Parse details (no extra round-trip)
        _update_mast_progress("fetching_details",
                              message=f"Parsing details for {len(post_items)} statuses...")
        details = await client.get_post_details_batch(post_items)
        logger.info("MAST: Parsed details for %d statuses", len(details))

        # Step 4: Upsert + snapshot
        new_activity_details: list[dict] = []
        poll_timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

        for idx, detail in enumerate(details, 1):
            _update_mast_progress("processing", current=idx, total=len(details),
                                  message=f"Processing status {idx}/{len(details)}...")
            try:
                uri = detail["post_uri"]
                likes = detail.get("likes", 0)
                reposts = detail.get("reposts", 0)
                replies = detail.get("replies", 0)
                quotes = detail.get("quotes", 0)

                # Check for stat increases to drive notifications
                prev = mast_queries.get_mast_submission(conn, uri)
                if prev and (likes > prev.get("likes", 0)
                             or reposts > prev.get("reposts", 0)):
                    new_activity_details.append({"title": detail.get("title", "")})

                mast_queries.upsert_mast_submission(conn, detail, account_id)
                mast_queries.insert_mast_snapshot(conn, account_id, uri, likes, reposts,
                                                  replies, quotes, polled_at=poll_timestamp)
                stats["snapshots_inserted"] += 1

            except Exception as e:
                logger.warning("Error processing MAST status %s: %s",
                               detail.get("post_uri", "")[:50], e, exc_info=True)

        conn.commit()

        # ── Inbox capture (gap G3 Stage A1) ───────────────────
        # /statuses/{id}/context per behind-looking status. The numeric status
        # id is the tail of the ActivityPub uri/URL (both shapes end /{id}).
        try:
            from polling.inbox_capture import capture as _inbox_capture

            async def _fetch_context(c):
                sid = str(c["submission_id"]).rstrip("/").rsplit("/", 1)[-1]
                if not sid.isdigit():
                    return []
                return await client.get_status_context(sid)

            await _inbox_capture(
                conn, "mast",
                [{"submission_id": d.get("post_uri", ""),
                  "fresh_count": d.get("replies", 0) or 0,
                  "title": d.get("title", "")} for d in details],
                _fetch_context, account_id=account_id,
                own_author=getattr(client, "_username", "") or "")
        except Exception as ce:  # noqa: BLE001 — capture never fails the poll
            logger.warning("MAST inbox capture failed: %s", ce)

        # ── Notifications ─────────────────────────────────────
        if is_first:
            logger.info("First MAST poll for account %s -- suppressing %d activity notifications",
                        account_id, len(new_activity_details))
        else:
            try:
                _send_mast_notifications(new_activity_details)
            except Exception as ne:
                logger.warning("Failed to send MAST notifications: %s", ne, exc_info=True)
            try:
                await _send_mast_telegram(new_activity_details)
            except Exception as te:
                logger.warning("Failed to send MAST Telegram notification: %s", te, exc_info=True)

        # Finalise
        # Follower count: reuse the authed client to snapshot the account's
        # follower total (network fetch first, then a short DB write — no lock
        # held across the await). Best-effort; never fails the cycle.
        await capture_followers(client, account_id, conn)

        duration = time.time() - start_time
        _update_mast_progress("complete", current=len(details), total=len(details),
                              message=f"Done -- {stats['submissions_found']} statuses in {duration:.1f}s")
        mast_queries.finish_mast_poll_log(conn, log_id, "success",
                                          duration_seconds=duration, **stats)
        logger.info("MAST poll complete in %.1fs -- %d statuses, %d snapshots",
                     duration, stats["submissions_found"], stats["snapshots_inserted"])

        # -- Telegram notifications ----------------------------------------
        if not is_first:
            from polling.telegram import send_poll_summary, check_milestones_batch, check_goals
            try:
                await send_poll_summary("mast", stats, duration)
            except Exception as te:
                logger.warning("Failed to send MAST Telegram summary: %s", te, exc_info=True)
            try:
                await check_milestones_batch("mast", "mast_snapshots", "mast_submissions", account_id)
            except Exception as me:
                logger.warning("Failed to check MAST milestones: %s", me, exc_info=True)
            try:
                await check_goals()
            except Exception as ge:
                logger.warning("Failed to check goals: %s", ge, exc_info=True)

        return stats

    except Exception as e:
        duration = time.time() - start_time
        _update_mast_progress("error", message=describe_error(e))
        logger.error("MAST poll failed: %s", describe_error(e), exc_info=True)
        if conn and log_id:
            mast_queries.finish_mast_poll_log(conn, log_id, "error",
                                              error_message=describe_error(e),
                                              duration_seconds=duration, **stats)
            conn.commit()
        from polling.telegram import send_poll_error
        try:
            await send_poll_error("mast", e)
        except Exception:
            logger.debug("Error alert send failed", exc_info=True)
        raise
    finally:
        _mast_first_poll_done.add(account_id)
        _mast_poll_running = False
        _mast_poll_lock.release()
        if conn:
            conn.close()
