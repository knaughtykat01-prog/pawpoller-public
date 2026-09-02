"""e621 (E621) poll cycle orchestration.

Official e621 REST API, HTTP Basic auth (username + API key). Poll-only.

Key differences from other pollers:
  - Uses E621Client with username + api_key
  - Settings keys: e621_username, e621_api_key
  - Metrics: score (score.total, can be negative), favorites_count, comments_count
  - No follower series (e621 exposes no per-user follower count)
  - Notifications: "E621:" prefix
"""

from __future__ import annotations
import atexit
import logging
import threading
import time
from datetime import datetime, timezone
from html import escape as _esc

import config
from clients.e621.client import E621Client
from database.db import get_connection
from polling.notifications import describe_error
from database import e621_queries
from polling import notifications

logger = logging.getLogger(__name__)

# -- Progress tracking --------------------------------------------------------
e621_poll_progress = {
    "active": False,
    "phase": "idle",
    "current": 0,
    "total": 0,
    "message": "",
}

_e621_poll_running = False
_e621_poll_lock = threading.Lock()
_e621_first_poll_done: set[int] = set()

# Persistent client — reused across poll cycles
_e621_client: E621Client | None = None


def _cleanup_e621_client():
    if _e621_client is not None:
        import asyncio
        try:
            asyncio.get_event_loop().run_until_complete(_e621_client.close())
        except Exception:
            logger.debug("e621 client cleanup failed", exc_info=True)


atexit.register(_cleanup_e621_client)


def _update_e621_progress(phase: str, current: int = 0, total: int = 0, message: str = ""):
    e621_poll_progress["active"] = phase not in ("idle", "complete", "error")
    e621_poll_progress["phase"] = phase
    e621_poll_progress["current"] = current
    e621_poll_progress["total"] = total
    e621_poll_progress["message"] = message


def _send_e621_notifications(new_details: list[dict]) -> None:
    """Send Windows toast notifications for e621 activity."""
    settings = config.get_settings()
    n = len(new_details)
    notifications.maybe_show_toast(
        settings,
        "e621_notifications_enabled",
        f"E621: {n} Post{'s' if n != 1 else ''} Updated",
        [f"{d['title'][:50]} gained activity" for d in new_details],
    )


async def _send_e621_telegram(new_details: list[dict]) -> None:
    """Send Telegram notification for e621 activity."""
    settings = config.get_settings()
    n = len(new_details)
    await notifications.maybe_send_telegram_summary(
        settings,
        f"<b>\U0001f43e E621: {n} Post{'s' if n != 1 else ''} Updated</b>",
        [_esc(d['title'][:50]) for d in new_details],
        log_label="E621",
    )


def _get_or_create_client(settings: dict, e621_username: str, e621_api_key: str) -> E621Client:
    """Return the persistent E621Client, re-pointed at the account's credentials."""
    global _e621_client

    if _e621_client is None:
        from polling.cf_proxy import proxy_kwargs
        _e621_client = E621Client(
            username=e621_username,
            api_key=e621_api_key,
            **proxy_kwargs(settings, "e621"),
        )
    else:
        _e621_client.update_credentials(e621_username, e621_api_key)

    return _e621_client


async def run_e621_poll_cycle(account_id: int | None = None, force_full: bool = False) -> dict:
    """Execute one complete e621 poll cycle for a single account."""
    global _e621_poll_running

    from database import accounts as accounts_db
    _ac = get_connection()
    try:
        if account_id is None:
            account_id = accounts_db.get_default_account_id(_ac, "e621", create=True)
        account_row = accounts_db.get_account(_ac, account_id)
    finally:
        _ac.close()
    is_default = bool(account_row["is_default"]) if account_row else True
    is_first = account_id not in _e621_first_poll_done

    if not _e621_poll_lock.acquire(blocking=False):
        logger.warning("e621 poll already running -- skipping (account %s)", account_id)
        return {}
    _e621_poll_running = True
    _update_e621_progress("starting", message="Initialising e621 poll cycle...")

    conn = None
    log_id = None
    start_time = time.time()

    stats = {
        "submissions_found": 0,
        "snapshots_inserted": 0,
    }

    settings = config.get_settings()
    creds = config.resolve_account_credentials("e621", account_id, is_default, settings)
    client = _get_or_create_client(settings, creds.get("e621_username", ""),
                                   creds.get("e621_api_key", ""))

    try:
        conn = get_connection()
        log_id = e621_queries.start_e621_poll_log(conn, account_id)

        # Step 1: Verify credentials
        _update_e621_progress("searching", message="Authenticating with e621...")
        name = await client.validate_session()
        if not name:
            raise ValueError("e621 auth failed -- check the username + API key")

        # Step 2: Discover uploads
        _update_e621_progress("searching", message="Fetching upload list...")
        post_items = await client.get_all_post_uris()
        stats["submissions_found"] = len(post_items)
        logger.info("e621: found %d posts", len(post_items))

        if not post_items:
            _update_e621_progress("complete", message="No e621 uploads found.")
            e621_queries.finish_e621_poll_log(conn, log_id, "success",
                                              duration_seconds=time.time() - start_time, **stats)
            conn.commit()
            return stats

        # Step 3: Parse details (no extra round-trip)
        _update_e621_progress("fetching_details",
                              message=f"Parsing details for {len(post_items)} posts...")
        details = await client.get_post_details_batch(post_items)
        logger.info("e621: parsed details for %d posts", len(details))

        # Step 4: Upsert + snapshot
        new_activity_details: list[dict] = []
        poll_timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

        for idx, detail in enumerate(details, 1):
            _update_e621_progress("processing", current=idx, total=len(details),
                                  message=f"Processing post {idx}/{len(details)}...")
            try:
                uri = detail["post_uri"]
                score = detail.get("score", 0)
                up_score = detail.get("up_score", 0)
                down_score = detail.get("down_score", 0)
                faves = detail.get("favorites_count", 0)
                comments = detail.get("comments_count", 0)

                prev = e621_queries.get_e621_submission(conn, uri)
                if prev and (faves > prev.get("favorites_count", 0)
                             or comments > prev.get("comments_count", 0)):
                    new_activity_details.append({"title": detail.get("title", "")})

                e621_queries.upsert_e621_submission(conn, detail, account_id)
                e621_queries.insert_e621_snapshot(conn, account_id, uri, score, faves,
                                                  comments, polled_at=poll_timestamp,
                                                  up_score=up_score, down_score=down_score)
                stats["snapshots_inserted"] += 1

            except Exception as e:
                logger.warning("Error processing e621 post %s: %s",
                               detail.get("post_uri", "")[:50], e, exc_info=True)

        conn.commit()

        # ── Inbox capture (gap G3 Stage A1) ───────────────────
        # /comments.json per behind-looking post (post_uri IS the numeric id).
        # Client enforces the ~1 req/s pacing; capture is capped per cycle.
        try:
            from polling.inbox_capture import capture as _inbox_capture

            async def _fetch_comments(c):
                return await client.get_comments(c["submission_id"])

            await _inbox_capture(
                conn, "e621",
                [{"submission_id": d.get("post_uri", ""),
                  "fresh_count": d.get("comments_count", 0) or 0,
                  "title": d.get("title", "")} for d in details],
                _fetch_comments, account_id=account_id,
                own_author=getattr(client, "username", "") or "")
        except Exception as ce:  # noqa: BLE001 — capture never fails the poll
            logger.warning("e621 inbox capture failed: %s", ce)

        # ── Notifications ─────────────────────────────────────
        if is_first:
            logger.info("First e621 poll for account %s -- suppressing %d activity notifications",
                        account_id, len(new_activity_details))
        else:
            try:
                _send_e621_notifications(new_activity_details)
            except Exception as ne:
                logger.warning("Failed to send e621 notifications: %s", ne, exc_info=True)
            try:
                await _send_e621_telegram(new_activity_details)
            except Exception as te:
                logger.warning("Failed to send e621 Telegram notification: %s", te, exc_info=True)

        duration = time.time() - start_time
        _update_e621_progress("complete", current=len(details), total=len(details),
                              message=f"Done -- {stats['submissions_found']} posts in {duration:.1f}s")
        e621_queries.finish_e621_poll_log(conn, log_id, "success",
                                          duration_seconds=duration, **stats)
        logger.info("e621 poll complete in %.1fs -- %d posts, %d snapshots",
                    duration, stats["submissions_found"], stats["snapshots_inserted"])

        # -- Telegram notifications ----------------------------------------
        if not is_first:
            from polling.telegram import send_poll_summary, check_milestones_batch, check_goals
            try:
                await send_poll_summary("e621", stats, duration)
            except Exception as te:
                logger.warning("Failed to send e621 Telegram summary: %s", te, exc_info=True)
            try:
                await check_milestones_batch("e621", "e621_snapshots", "e621_submissions", account_id)
            except Exception as me:
                logger.warning("Failed to check e621 milestones: %s", me, exc_info=True)
            try:
                await check_goals()
            except Exception as ge:
                logger.warning("Failed to check goals: %s", ge, exc_info=True)

        return stats

    except Exception as e:
        duration = time.time() - start_time
        _update_e621_progress("error", message=describe_error(e))
        logger.error("e621 poll failed: %s", describe_error(e), exc_info=True)
        if conn and log_id:
            e621_queries.finish_e621_poll_log(conn, log_id, "error",
                                              error_message=describe_error(e),
                                              duration_seconds=duration, **stats)
            conn.commit()
        from polling.telegram import send_poll_error
        try:
            await send_poll_error("e621", e)
        except Exception:
            logger.debug("Error alert send failed", exc_info=True)
        raise
    finally:
        _e621_first_poll_done.add(account_id)
        _e621_poll_running = False
        _e621_poll_lock.release()
        if conn:
            conn.close()
