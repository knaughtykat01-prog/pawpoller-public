"""SquidgeWorld (SqW) poll cycle orchestration.

Mirrors the SoFurry poller pattern since the auth flow is similar
(username/password login), but with OTW Archive-specific data:
hits, kudos, comments, bookmarks, plus individual kudos user tracking.

Key differences:
  - Authentication via OTW Archive Rails login (CSRF token + form POST)
  - Work IDs are integers
  - Tracks bookmarks (unique to OTW Archive)
  - Tracks individual kudos users (like IB's faving_users)
"""

from __future__ import annotations
import atexit
import logging
import threading
import time
from datetime import datetime, timezone
from html import escape as _esc

import config
from clients.sqw.client import SquidgeWorldClient
from database.db import get_connection
from polling.notifications import describe_error
from database import sqw_queries
from polling import notifications

logger = logging.getLogger(__name__)

# -- Progress tracking -------------------------------------------------
sqw_poll_progress = {
    "active": False,
    "phase": "idle",
    "current": 0,
    "total": 0,
    "message": "",
}

_sqw_poll_running = False
_sqw_poll_lock = threading.Lock()
_sqw_first_poll_done: set[int] = set()

# Persistent client — reused across poll cycles
_sqw_client: SquidgeWorldClient | None = None


def _cleanup_sqw_client():
    if _sqw_client is not None:
        import asyncio
        try:
            asyncio.get_event_loop().run_until_complete(_sqw_client.close())
        except Exception:
            logger.debug("Error alert send failed", exc_info=True)


atexit.register(_cleanup_sqw_client)


def _update_sqw_progress(phase: str, current: int = 0, total: int = 0, message: str = ""):
    sqw_poll_progress["active"] = phase not in ("idle", "complete", "error")
    sqw_poll_progress["phase"] = phase
    sqw_poll_progress["current"] = current
    sqw_poll_progress["total"] = total
    sqw_poll_progress["message"] = message


def _send_sqw_notifications(new_details: list[dict]) -> None:
    """Send Windows toast notifications for SquidgeWorld activity."""
    settings = config.get_settings()
    n = len(new_details)
    notifications.maybe_show_toast(
        settings,
        "sqw_notifications_enabled",
        f"SqW: {n} Work{'s' if n != 1 else ''} Updated",
        [f"{d['title']} gained activity" for d in new_details],
    )


async def _send_sqw_telegram(new_details: list[dict]) -> None:
    """Send Telegram notification for SquidgeWorld activity."""
    settings = config.get_settings()
    n = len(new_details)
    await notifications.maybe_send_telegram_summary(
        settings,
        f"<b>SqW: {n} Work{'s' if n != 1 else ''} Updated</b>",
        [_esc(d['title']) for d in new_details],
        log_label="SqW",
    )


def _send_sqw_kudos_notifications(new_kudos: list[dict]) -> None:
    """Send Windows toast notifications for new kudos."""
    settings = config.get_settings()
    n = len(new_kudos)
    notifications.maybe_show_toast(
        settings,
        "sqw_notifications_enabled",
        f"SqW: {n} New Kudo{'s' if n != 1 else ''}",
        [f"{d['username']} left kudos on {d['title']}" for d in new_kudos],
    )


async def _send_sqw_kudos_telegram(new_kudos: list[dict]) -> None:
    """Send Telegram notification for new kudos."""
    settings = config.get_settings()
    n = len(new_kudos)
    await notifications.maybe_send_telegram_summary(
        settings,
        f"<b>🦑 SqW: {n} New Kudo{'s' if n != 1 else ''}</b>",
        [f"{_esc(d['username'])} → {_esc(d['title'])}" for d in new_kudos],
        log_label="SqW kudos",
    )


def _get_or_create_client(settings: dict, sqw_user: str, sqw_pass: str, sqw_target: str) -> SquidgeWorldClient:
    """Return the persistent SquidgeWorldClient, re-pointed at the account's creds."""
    global _sqw_client

    if _sqw_client is None:
        from polling.cf_proxy import proxy_kwargs
        _sqw_client = SquidgeWorldClient(
            username=sqw_user,
            password=sqw_pass,
            target_user=sqw_target,
            **proxy_kwargs(settings, "sqw"),
        )
    else:
        _sqw_client.update_credentials(sqw_user, sqw_pass, sqw_target)

    return _sqw_client


async def run_sqw_poll_cycle(account_id: int | None = None, force_full: bool = False) -> dict:
    """Execute one complete SquidgeWorld poll cycle for a single account.

    Steps:
      1. Login and validate session
      2. Discover all works for the target user
      3. Fetch details for each work
      4. Upsert works and record snapshots
      5. Track kudos users
    """
    global _sqw_poll_running

    from database import accounts as accounts_db
    _ac = get_connection()
    try:
        if account_id is None:
            account_id = accounts_db.get_default_account_id(_ac, "sqw", create=True)
        account_row = accounts_db.get_account(_ac, account_id)
    finally:
        _ac.close()
    is_default = bool(account_row["is_default"]) if account_row else True
    is_first = account_id not in _sqw_first_poll_done

    if not _sqw_poll_lock.acquire(blocking=False):
        logger.warning("SqW poll already running -- skipping (account %s)", account_id)
        return {}
    _sqw_poll_running = True
    _update_sqw_progress("starting", message="Initialising SquidgeWorld poll cycle...")

    conn = None
    log_id = None
    start_time = time.time()

    stats = {
        "submissions_found": 0,
        "snapshots_inserted": 0,
        "new_kudos_found": 0,
    }

    settings = config.get_settings()
    creds = config.resolve_account_credentials("sqw", account_id, is_default, settings)
    client = _get_or_create_client(settings, creds.get("sqw_username", ""),
                                   creds.get("sqw_password", ""), creds.get("sqw_target_user", ""))

    try:
        conn = get_connection()
        log_id = sqw_queries.start_sqw_poll_log(conn, account_id)
        # Step 1: Authenticate
        _update_sqw_progress("searching", message="Authenticating with SquidgeWorld...")
        # Reset login state so ensure_logged_in() attempts a fresh login
        client._logged_in = False
        target = await client.validate_session()
        if not target:
            raise ValueError("SquidgeWorld login failed -- check credentials")

        # Step 2: Discover works
        _update_sqw_progress("searching", message="Fetching works list...")
        works = await client.get_all_work_ids()
        work_ids = [w["work_id"] for w in works]
        stats["submissions_found"] = len(work_ids)
        logger.info("SqW: Found %d works", len(work_ids))

        if not work_ids:
            _update_sqw_progress("complete", message="No SquidgeWorld works found.")
            sqw_queries.finish_sqw_poll_log(conn, log_id, "success",
                                             duration_seconds=time.time() - start_time, **stats)
            conn.commit()
            return stats

        # Step 3: Fetch details
        _update_sqw_progress("fetching_details",
                             message=f"Fetching details for {len(work_ids)} works...")
        details = await client.get_work_details_batch(work_ids)
        logger.info("SqW: Fetched details for %d works", len(details))

        # Step 4: Upsert + snapshot
        poll_timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        new_kudos_details: list[dict] = []

        for idx, detail in enumerate(details, 1):
            _update_sqw_progress("processing", current=idx, total=len(details),
                                 message=f"Processing work {idx}/{len(details)}...")
            try:
                wid = detail["work_id"]
                views = detail.get("views", 0)
                faves = detail.get("favorites_count", 0)
                comments = detail.get("comments_count", 0)
                bookmarks = detail.get("bookmarks_count", 0)

                # Defence in depth against a transient scrape failure writing a
                # 0-view snapshot: OTW "hits" are cumulative and never drop, so
                # a scraped 0 when the DB already holds a non-zero count means
                # the fetch/parse failed, not that the work reset. Persisting it
                # corrupts the baseline and inflates the next digest/milestone
                # delta. Skip the work this cycle; the next one re-reads it.
                if views == 0:
                    existing = sqw_queries.get_sqw_submission(conn, wid)
                    if existing and (existing.get("views") or 0) > 0:
                        logger.warning(
                            "SqW: skipping work %s — scraped 0 views but DB has "
                            "%d (transient fetch/parse failure, not a reset)",
                            wid, existing["views"],
                        )
                        continue

                sqw_queries.upsert_sqw_submission(conn, detail, account_id)
                sqw_queries.insert_sqw_snapshot(conn, account_id, wid, views, faves, comments,
                                                bookmarks, polled_at=poll_timestamp)
                stats["snapshots_inserted"] += 1
                # Commit before the kudos fetch below: holding the implicit
                # write transaction across its await blocks every other
                # poller's writes past the 30s busy_timeout.
                conn.commit()

                # Step 5: Track kudos users
                try:
                    kudos_users = await client.get_kudos_users(wid)
                    # Batch insert: get existing usernames first to identify new ones
                    existing_usernames = {r["username"] for r in sqw_queries.get_sqw_kudos_users(conn, wid)}
                    new_count = sqw_queries.upsert_sqw_kudos_users_batch(conn, account_id, wid, kudos_users)
                    conn.commit()
                    stats["new_kudos_found"] += new_count
                    for ku in kudos_users:
                        if ku not in existing_usernames:
                            new_kudos_details.append({
                                "username": ku,
                                "title": detail.get("title", ""),
                            })
                except Exception as ke:
                    logger.warning("Error fetching kudos for work %s: %s", wid, ke, exc_info=True)

            except Exception as e:
                logger.warning("Error processing SqW work %s: %s",
                               detail.get("work_id"), e, exc_info=True)

        conn.commit()

        # ── Notifications (kudos) ────────────────────────────
        if is_first:
            logger.info("First SqW poll for account %s -- suppressing %d kudos notifications",
                        account_id, len(new_kudos_details))
        else:
            if new_kudos_details:
                try:
                    _send_sqw_kudos_notifications(new_kudos_details)
                except Exception as ne:
                    logger.warning("Failed to send SqW kudos notifications: %s", ne, exc_info=True)
                try:
                    await _send_sqw_kudos_telegram(new_kudos_details)
                except Exception as te:
                    logger.warning("Failed to send SqW kudos Telegram: %s", te, exc_info=True)

        # Finalise
        duration = time.time() - start_time
        _update_sqw_progress("complete", current=len(details), total=len(details),
                             message=f"Done -- {stats['submissions_found']} works, "
                                     f"{stats['new_kudos_found']} new kudos in {duration:.1f}s")
        sqw_queries.finish_sqw_poll_log(conn, log_id, "success",
                                         duration_seconds=duration, **stats)
        logger.info("SqW poll complete in %.1fs -- %d works, %d snapshots, %d new kudos",
                     duration, stats["submissions_found"], stats["snapshots_inserted"],
                     stats["new_kudos_found"])

        # ── Telegram notifications ────────────────────────────
        if not is_first:
            from polling.telegram import send_poll_summary, check_milestones_batch, check_goals
            try:
                await send_poll_summary("sqw", stats, duration)
            except Exception as te:
                logger.warning("Failed to send SqW Telegram summary: %s", te, exc_info=True)
            try:
                await check_milestones_batch("sqw", "sqw_snapshots", "sqw_submissions", account_id)
            except Exception as me:
                logger.warning("Failed to check SqW milestones: %s", me, exc_info=True)
            try:
                await check_goals()
            except Exception as ge:
                logger.warning("Failed to check goals: %s", ge, exc_info=True)

        return stats

    except Exception as e:
        duration = time.time() - start_time
        _update_sqw_progress("error", message=describe_error(e))
        logger.error("SqW poll failed: %s", describe_error(e), exc_info=True)
        if conn and log_id:
            sqw_queries.finish_sqw_poll_log(conn, log_id, "error",
                                             error_message=describe_error(e),
                                             duration_seconds=duration, **stats)
            conn.commit()
        from polling.telegram import send_poll_error
        try:
            await send_poll_error("sqw", e)
        except Exception:
            logger.debug("Error alert send failed", exc_info=True)
        raise
    finally:
        _sqw_first_poll_done.add(account_id)
        _sqw_poll_running = False
        _sqw_poll_lock.release()
        if conn:
            conn.close()
