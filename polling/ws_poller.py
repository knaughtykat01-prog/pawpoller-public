"""Weasyl (WS) poll cycle orchestration.

This is the simplest of the three platform pollers.  Weasyl's public API
provides submission metadata (views, faves, comments counts) but does
**not** expose:

  - **Faving user lists** -- there is no endpoint to discover *who* faved
    a submission, so there is no fave-user tracking step at all.
  - **Comment content or authors** -- the API returns a comment *count*
    but not the individual comments, so there is no comment-scraping step.

As a result the poll cycle is just three steps:
  1. Validate the API key and resolve the username.
  2. Discover all gallery submissions.
  3. Fetch details and record snapshots (views / faves / comments counts).

Notifications are basic "submission updated" alerts rather than the
per-user fave/comment breakdowns that IB and FA provide.
"""

from __future__ import annotations
import logging
import threading
import time
from datetime import datetime, timezone
from html import escape as _esc

import config
from clients.weasyl.client import WeasylClient
from database.db import get_connection
from polling.notifications import describe_error
from polling.followers import capture_followers
from database import ws_queries
from polling import notifications

logger = logging.getLogger(__name__)

# ── Progress tracking ────────────────────────────────────────
# Same shared-dict pattern as the IB and FA pollers, read by the
# /api/ws/poll/progress endpoint.
ws_poll_progress = {
    "active": False,
    "phase": "idle",
    "current": 0,
    "total": 0,
    "message": "",
}

# Concurrency guard -- identical pattern to the other pollers.
# The Lock protects the check-and-set from race conditions; the
# boolean remains as a readable status indicator.
_ws_poll_running = False
_ws_poll_lock = threading.Lock()
# Per-account first-poll suppression (single lock still serialises WS polls —
# a platform's accounts poll sequentially).
_ws_first_poll_done: set[int] = set()


def _update_ws_progress(phase: str, current: int = 0, total: int = 0, message: str = ""):
    """Mutate the shared ws_poll_progress dict for the frontend.
    Same pattern as _update_progress() in the IB poller."""
    ws_poll_progress["active"] = phase not in ("idle", "complete", "error")
    ws_poll_progress["phase"] = phase
    ws_poll_progress["current"] = current
    ws_poll_progress["total"] = total
    ws_poll_progress["message"] = message


def _send_ws_notifications(new_details: list[dict], detail_type: str = "activity") -> None:
    """Send Windows toast notifications for Weasyl activity.

    Generic "submission gained activity" alerts (Weasyl's API doesn't
    expose *who* faved or commented). ``ws_notification_comments_only``
    suppresses these entirely since WS activity is fave-count-driven and
    has no separate comment-alert path.
    """
    settings = config.get_settings()
    if settings.get("ws_notification_comments_only", False):
        return
    n = len(new_details)
    notifications.maybe_show_toast(
        settings,
        "ws_notifications_enabled",
        f"WS: {n} Submission{'s' if n != 1 else ''} Updated",
        [f"{d['title']} gained activity" for d in new_details],
    )


async def _send_ws_telegram(new_details: list[dict]) -> None:
    """Send Telegram notification for Weasyl activity.

    Title-only bullets since WS API doesn't expose per-user interaction
    data. Same comments_only filter as the toast path.
    """
    settings = config.get_settings()
    if settings.get("ws_notification_comments_only", False):
        return
    n = len(new_details)
    await notifications.maybe_send_telegram_summary(
        settings,
        f"<b>🦎 WS: {n} Submission{'s' if n != 1 else ''} Updated</b>",
        [_esc(d['title']) for d in new_details],
        log_label="WS",
    )


async def run_ws_poll_cycle(account_id: int | None = None, force_full: bool = False) -> dict:
    """Execute one complete Weasyl poll cycle for a single account.

    This is the most streamlined of the three pollers because Weasyl's API
    does not provide user-level fave or comment data.  The cycle is:

      1. **Validate API key** -- call the whoami endpoint to confirm the
         key is valid and resolve the username.  This is a prerequisite
         before any gallery fetch; an invalid key raises immediately.
      2. **Gallery discovery** -- paginate through the user's gallery to
         collect all submission IDs.
      3. **Detail fetch**      -- batch-fetch metadata for each submission.
      4. **Upsert + snapshot** -- write/update submission rows and record
                                  point-in-time stats (views, faves, comments).

    There are **no fave-user or comment steps** -- the stats dict only has
    ``submissions_found`` and ``snapshots_inserted``.

    The ``force_full`` parameter is accepted for interface consistency with
    the IB and FA pollers but has no special effect here since there are no
    conditional fetch steps to force.

    Args:
        force_full: Accepted for API consistency but currently unused.

    Returns:
        Stats dict with keys: submissions_found, snapshots_inserted.
        Empty dict if a poll was already running.
    """
    global _ws_poll_running

    # ── Resolve the account to poll (default WS account when unspecified) ──
    from database import accounts as accounts_db
    _ac = get_connection()
    try:
        if account_id is None:
            account_id = accounts_db.get_default_account_id(_ac, "ws", create=True)
        account_row = accounts_db.get_account(_ac, account_id)
    finally:
        _ac.close()
    is_default = bool(account_row["is_default"]) if account_row else True
    is_first = account_id not in _ws_first_poll_done

    # Concurrency guard -- one WS poll at a time (accounts poll sequentially).
    if not _ws_poll_lock.acquire(blocking=False):
        logger.warning("WS poll already running — skipping (account %s)", account_id)
        return {}
    _ws_poll_running = True
    _update_ws_progress("starting", message="Initialising Weasyl poll cycle...")

    conn = None
    log_id = None
    start_time = time.time()

    # Minimal stats dict -- no fave or comment tracking for Weasyl.
    stats = {
        "submissions_found": 0,
        "snapshots_inserted": 0,
    }

    settings = config.get_settings()
    creds = config.resolve_account_credentials("ws", account_id, is_default, settings)
    from polling.cf_proxy import proxy_kwargs
    client = WeasylClient(api_key=creds.get("ws_api_key", ""),
                          **proxy_kwargs(settings, "ws"))

    try:
        conn = get_connection()
        log_id = ws_queries.start_ws_poll_log(conn, account_id)
        # ── Step 1: Validate API key ───────────────────────────
        # The Weasyl API requires a valid API key for all requests.
        # We call validate_key() first to fail fast with a clear error
        # rather than getting cryptic 401s during the gallery fetch.
        _update_ws_progress("searching", message="Validating API key and fetching gallery...")
        username = await client.validate_key()
        if not username:
            raise ValueError("Weasyl API key is invalid or not set")

        # ── Step 2: Discover gallery submissions ───────────────
        gallery = await client.get_all_gallery_ids()
        submission_ids = [s["submission_id"] for s in gallery]
        stats["submissions_found"] = len(submission_ids)
        logger.info("WS: Found %d submissions", len(submission_ids))

        if not submission_ids:
            _update_ws_progress("complete", message="No Weasyl submissions found.")
            ws_queries.finish_ws_poll_log(conn, log_id, "success", duration_seconds=time.time() - start_time, **stats)
            conn.commit()
            return stats

        # ── Step 3: Fetch details for each submission ──────────
        _update_ws_progress("fetching_details", message=f"Fetching details for {len(submission_ids)} submissions...")
        details = await client.get_submission_details_batch(submission_ids)
        logger.info("WS: Fetched details for %d submissions", len(details))

        # ── Step 4: Upsert submissions and insert snapshots ────
        # This is the final step -- no conditional fave/comment fetching.
        # We just record the aggregate counts for historical charting.
        new_activity_details: list[dict] = []
        poll_timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        for idx, detail in enumerate(details, 1):
            _update_ws_progress("processing", current=idx, total=len(details),
                                message=f"Processing submission {idx}/{len(details)}...")
            try:
                sub_id = detail["submission_id"]
                views = detail.get("views", 0)
                faves = detail.get("favorites_count", 0)
                comments = detail.get("comments_count", 0)

                # Check for stat increases to drive notifications.
                prev_faves = ws_queries.get_ws_previous_favorites_count(conn, sub_id)
                if prev_faves is not None and faves > prev_faves:
                    new_activity_details.append({"title": detail.get("title", "")})

                ws_queries.upsert_ws_submission(conn, detail, account_id)
                ws_queries.insert_ws_snapshot(conn, account_id, sub_id, views, faves, comments, polled_at=poll_timestamp)
                stats["snapshots_inserted"] += 1

            except Exception as e:
                # Per-submission error handling -- same resilience pattern
                # as IB/FA: log and continue with the next submission.
                logger.warning("Error processing WS submission %s: %s", detail.get("submission_id"), e, exc_info=True)

        conn.commit()

        # ── Notifications ─────────────────────────────────────
        if is_first:
            logger.info("First WS poll for account %s — suppressing %d activity notifications",
                        account_id, len(new_activity_details))
        else:
            try:
                _send_ws_notifications(new_activity_details)
            except Exception as ne:
                logger.warning("Failed to send WS notifications: %s", ne, exc_info=True)
            try:
                await _send_ws_telegram(new_activity_details)
            except Exception as te:
                logger.warning("Failed to send WS Telegram notification: %s", te, exc_info=True)

        # ── Finalise ───────────────────────────────────────────
        # Follower count: reuse the authed client to snapshot the account's
        # follower total (network fetch first, then a short DB write — no lock
        # held across the await). Best-effort; never fails the cycle.
        await capture_followers(client, account_id, conn)

        duration = time.time() - start_time
        _update_ws_progress("complete", current=len(details), total=len(details),
                            message=f"Done — {stats['submissions_found']} submissions in {duration:.1f}s")
        ws_queries.finish_ws_poll_log(conn, log_id, "success", duration_seconds=duration, **stats)
        logger.info("WS poll complete in %.1fs — %d submissions, %d snapshots",
                     duration, stats["submissions_found"], stats["snapshots_inserted"])

        # ── Telegram notifications ────────────────────────────
        if not is_first:
            from polling.telegram import send_poll_summary, check_milestones_batch, check_goals
            try:
                await send_poll_summary("ws", stats, duration)
            except Exception as te:
                logger.warning("Failed to send WS Telegram summary: %s", te, exc_info=True)
            try:
                await check_milestones_batch("ws", "ws_snapshots", "ws_submissions", account_id)
            except Exception as me:
                logger.warning("Failed to check WS milestones: %s", me, exc_info=True)
            try:
                await check_goals()
            except Exception as ge:
                logger.warning("Failed to check goals: %s", ge, exc_info=True)

        return stats

    except Exception as e:
        # Top-level failure -- record partial stats and propagate.
        duration = time.time() - start_time
        _update_ws_progress("error", message=describe_error(e))
        logger.error("WS poll failed: %s", describe_error(e), exc_info=True)
        if conn and log_id:
            ws_queries.finish_ws_poll_log(conn, log_id, "error", error_message=describe_error(e), duration_seconds=duration, **stats)
            conn.commit()
        # Send error alert via Telegram
        from polling.telegram import send_poll_error
        try:
            await send_poll_error("ws", e)
        except Exception:
            logger.debug("Error alert send failed", exc_info=True)
        raise
    finally:
        # Always clear the guard and release resources.
        _ws_first_poll_done.add(account_id)
        _ws_poll_running = False
        _ws_poll_lock.release()
        await client.close()
        if conn:
            conn.close()
