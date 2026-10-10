"""Rule34.xxx poll cycle (spec 028 US6): tracking only, never posts.

Reads the account's uploads plus its tracked artist tags and characters from Rule34's API host with the
account's own API key and user id (settings ``r34_username``, ``r34_api_key``, ``r34_user_id``). Rule34
shares score and comment counts only. Same shape as the Furbooru poller; tracking goes through
``polling/board_track.py`` like e621 and Furbooru.
"""

from __future__ import annotations
import atexit
import logging
import threading
import time
from datetime import datetime, timezone
from html import escape as _esc

import config
from polling import loop_bound
from clients.r34.client import Rule34Client
from database.db import get_connection
from polling.notifications import describe_error
from database import r34_queries
from polling import notifications

logger = logging.getLogger(__name__)

# -- Progress tracking --------------------------------------------------------
r34_poll_progress = {
    "active": False,
    "phase": "idle",
    "current": 0,
    "total": 0,
    "message": "",
}

_r34_poll_running = False
_r34_poll_lock = threading.Lock()
_r34_first_poll_done: set[int] = set()

# Persistent client — reused across poll cycles
_r34_client: Rule34Client | None = None


def _cleanup_r34_client():
    if _r34_client is not None:
        import asyncio
        try:
            asyncio.get_event_loop().run_until_complete(_r34_client.close())
        except Exception:
            logger.debug("r34 client cleanup failed", exc_info=True)


atexit.register(_cleanup_r34_client)


def _update_r34_progress(phase: str, current: int = 0, total: int = 0, message: str = ""):
    r34_poll_progress["active"] = phase not in ("idle", "complete", "error")
    r34_poll_progress["phase"] = phase
    r34_poll_progress["current"] = current
    r34_poll_progress["total"] = total
    r34_poll_progress["message"] = message


def _send_r34_notifications(new_details: list[dict]) -> None:
    """Send Windows toast notifications for r34 activity."""
    settings = config.get_settings()
    n = len(new_details)
    notifications.maybe_show_toast(
        settings,
        "r34_notifications_enabled",
        f"Rule34: {n} Post{'s' if n != 1 else ''} Updated",
        [f"{d['title'][:50]} gained activity" for d in new_details],
    )


async def _send_r34_telegram(new_details: list[dict]) -> None:
    """Send Telegram notification for r34 activity."""
    settings = config.get_settings()
    n = len(new_details)
    await notifications.maybe_send_telegram_summary(
        settings,
        f"<b>\U0001f43e Rule34: {n} Post{'s' if n != 1 else ''} Updated</b>",
        [_esc(d['title'][:50]) for d in new_details],
        log_label="Rule34",
    )


def _get_or_create_client(settings: dict, r34_username: str, r34_api_key: str,
                          r34_user_id: str = "") -> Rule34Client:
    """Return the persistent Rule34Client, re-pointed at the account's credentials."""
    global _r34_client

    if not loop_bound.reusable(_r34_client):
        _r34_client = Rule34Client(username=r34_username, api_key=r34_api_key, user_id=r34_user_id)
    else:
        _r34_client.update_credentials(r34_username, r34_api_key, r34_user_id)

    return loop_bound.pin(_r34_client)


async def run_r34_poll_cycle(account_id: int | None = None, force_full: bool = False) -> dict:
    """Execute one complete r34 poll cycle for a single account."""
    global _r34_poll_running

    from database import accounts as accounts_db
    _ac = get_connection()
    try:
        if account_id is None:
            account_id = accounts_db.get_default_account_id(_ac, "r34", create=True)
        account_row = accounts_db.get_account(_ac, account_id)
    finally:
        _ac.close()
    is_default = bool(account_row["is_default"]) if account_row else True
    is_first = account_id not in _r34_first_poll_done

    if not _r34_poll_lock.acquire(blocking=False):
        logger.warning("r34 poll already running -- skipping (account %s)", account_id)
        return {}
    _r34_poll_running = True
    _update_r34_progress("starting", message="Initialising r34 poll cycle...")

    conn = None
    log_id = None
    start_time = time.time()

    stats = {
        "submissions_found": 0,
        "snapshots_inserted": 0,
    }

    settings = config.get_settings()
    creds = config.resolve_account_credentials("r34", account_id, is_default, settings)
    client = _get_or_create_client(settings, creds.get("r34_username", ""),
                                   creds.get("r34_api_key", ""), creds.get("r34_user_id", ""))

    try:
        conn = get_connection()
        log_id = r34_queries.start_r34_poll_log(conn, account_id)

        # Step 1: Verify credentials
        _update_r34_progress("searching", message="Authenticating with r34...")
        name = await client.validate_session()
        if not name:
            raise ValueError("Rule34 refused the API key and user id -- copy both again from Rule34: "
                             "My Account -> Options -> API Access Credentials")

        # Step 2: Discover uploads
        from polling import board_track
        track = await board_track.prepare(conn, "r34", client, account_id, name, settings)
        _update_r34_progress("searching", message="Fetching upload list...")
        post_items = (await client.get_all_post_uris(track["queries"], known=track["known"])
                      if track["extras"] else await client.get_all_post_uris())
        stats["submissions_found"] = len(post_items)
        logger.info("r34: found %d posts", len(post_items))

        if not post_items:
            _update_r34_progress("complete", message="No r34 uploads found.")
            r34_queries.finish_r34_poll_log(conn, log_id, "success",
                                              duration_seconds=time.time() - start_time, **stats)
            conn.commit()
            return stats

        # Step 3: Parse details (no extra round-trip)
        _update_r34_progress("fetching_details",
                              message=f"Parsing details for {len(post_items)} posts...")
        details = await client.get_post_details_batch(post_items)
        logger.info("r34: parsed details for %d posts", len(details))

        # Step 4: Upsert + snapshot
        new_activity_details: list[dict] = []
        found_new: list[dict] = []
        poll_timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

        for idx, detail in enumerate(details, 1):
            _update_r34_progress("processing", current=idx, total=len(details),
                                  message=f"Processing post {idx}/{len(details)}...")
            try:
                uri = detail["post_uri"]
                score = detail.get("score", 0)
                up_score = detail.get("up_score", 0)
                down_score = detail.get("down_score", 0)
                faves = detail.get("favorites_count", 0)
                comments = detail.get("comments_count", 0)

                prev = r34_queries.get_r34_submission(conn, uri)
                if prev and (faves > prev.get("favorites_count", 0)
                             or comments > prev.get("comments_count", 0)):
                    new_activity_details.append({"title": detail.get("title", "")})

                res = board_track.store(conn, "r34", detail, account_id, track, name,
                                        client.user_id, poll_timestamp, track["quiet"])
                if not res["shared"]:
                    stats["snapshots_inserted"] += 1
                if res.get("found_at"):
                    found_new.append({"reasons": res["reasons"],
                                      "uploader": detail.get("uploader_name") or ""})

            except Exception as e:
                logger.warning("Error processing r34 post %s: %s",
                               detail.get("post_uri", "")[:50], e, exc_info=True)

        conn.commit()
        board_track.finish(account_id, track)
        # (No inbox capture — the Philomena read API has no per-image comments
        # fetch wired here; Rule34 is poll-only for engagement counts.)

        # ── Notifications ─────────────────────────────────────
        if is_first:
            logger.info("First r34 poll for account %s -- suppressing %d activity notifications",
                        account_id, len(new_activity_details))
        else:
            try:
                _send_r34_notifications(new_activity_details)
            except Exception as ne:
                logger.warning("Failed to send r34 notifications: %s", ne, exc_info=True)
            try:
                await _send_r34_telegram(new_activity_details)
            except Exception as te:
                logger.warning("Failed to send r34 Telegram notification: %s", te, exc_info=True)
            try:
                await board_track.notify("r34", found_new)
            except Exception as fe:
                logger.warning("Failed to send r34 found-post notification: %s", fe, exc_info=True)

        duration = time.time() - start_time
        _update_r34_progress("complete", current=len(details), total=len(details),
                              message=f"Done -- {stats['submissions_found']} posts in {duration:.1f}s")
        r34_queries.finish_r34_poll_log(conn, log_id, "success",
                                          duration_seconds=duration, **stats)
        logger.info("r34 poll complete in %.1fs -- %d posts, %d snapshots",
                    duration, stats["submissions_found"], stats["snapshots_inserted"])

        # -- Telegram notifications ----------------------------------------
        if not is_first:
            from polling.telegram import send_poll_summary, check_milestones_batch, check_goals
            try:
                await send_poll_summary("r34", stats, duration)
            except Exception as te:
                logger.warning("Failed to send r34 Telegram summary: %s", te, exc_info=True)
            try:
                await check_milestones_batch("r34", "r34_snapshots", "r34_submissions", account_id)
            except Exception as me:
                logger.warning("Failed to check r34 milestones: %s", me, exc_info=True)
            try:
                await check_goals()
            except Exception as ge:
                logger.warning("Failed to check goals: %s", ge, exc_info=True)

        return stats

    except Exception as e:
        duration = time.time() - start_time
        _update_r34_progress("error", message=describe_error(e))
        logger.error("r34 poll failed: %s", describe_error(e), exc_info=True)
        if conn and log_id:
            r34_queries.finish_r34_poll_log(conn, log_id, "error",
                                              error_message=describe_error(e),
                                              duration_seconds=duration, **stats)
            conn.commit()
        from polling.telegram import send_poll_error
        try:
            await send_poll_error("r34", e)
        except Exception:
            logger.debug("Error alert send failed", exc_info=True)
        raise
    finally:
        _r34_first_poll_done.add(account_id)
        _r34_poll_running = False
        _r34_poll_lock.release()
        if conn:
            conn.close()
