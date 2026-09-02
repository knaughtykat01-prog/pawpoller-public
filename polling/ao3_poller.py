"""Archive of Our Own (AO3) poll cycle orchestration.

Mirrors the SquidgeWorld poller since both run on OTW Archive software.
AO3 uses Cloudflare instead of Anubis, with a higher rate limit (3s)
since it's a volunteer-run site.

Key differences from SqW:
  - No Anubis challenge solver
  - Cloudflare 403/429 handling in the client
  - Higher rate limiting (3s between requests)
"""

from __future__ import annotations
import atexit
import logging
import threading
import time
from datetime import datetime, timezone
from html import escape as _esc

import config
from clients.ao3.client import AO3Client
from database.db import get_connection
from polling.notifications import describe_error
from database import ao3_queries
from polling import notifications

logger = logging.getLogger(__name__)

# -- Progress tracking -------------------------------------------------
ao3_poll_progress = {
    "active": False,
    "phase": "idle",
    "current": 0,
    "total": 0,
    "message": "",
}

_ao3_poll_running = False
_ao3_poll_lock = threading.Lock()
_ao3_first_poll_done: set[int] = set()

# Persistent client — reused across poll cycles
_ao3_client: AO3Client | None = None


def _cleanup_ao3_client():
    if _ao3_client is not None:
        import asyncio
        try:
            asyncio.get_event_loop().run_until_complete(_ao3_client.close())
        except Exception:
            logger.debug("Error alert send failed", exc_info=True)


atexit.register(_cleanup_ao3_client)


def _update_ao3_progress(phase: str, current: int = 0, total: int = 0, message: str = ""):
    ao3_poll_progress["active"] = phase not in ("idle", "complete", "error")
    ao3_poll_progress["phase"] = phase
    ao3_poll_progress["current"] = current
    ao3_poll_progress["total"] = total
    ao3_poll_progress["message"] = message


def _send_ao3_notifications(new_details: list[dict]) -> None:
    """Send Windows toast notifications for AO3 activity."""
    settings = config.get_settings()
    n = len(new_details)
    notifications.maybe_show_toast(
        settings,
        "ao3_notifications_enabled",
        f"AO3: {n} Work{'s' if n != 1 else ''} Updated",
        [f"{d['title']} gained activity" for d in new_details],
    )


async def _send_ao3_telegram(new_details: list[dict]) -> None:
    """Send Telegram notification for AO3 activity."""
    settings = config.get_settings()
    n = len(new_details)
    await notifications.maybe_send_telegram_summary(
        settings,
        f"<b>AO3: {n} Work{'s' if n != 1 else ''} Updated</b>",
        [_esc(d['title']) for d in new_details],
        log_label="AO3",
    )


def _send_ao3_kudos_notifications(new_kudos: list[dict]) -> None:
    """Send Windows toast notifications for new kudos."""
    settings = config.get_settings()
    n = len(new_kudos)
    notifications.maybe_show_toast(
        settings,
        "ao3_notifications_enabled",
        f"AO3: {n} New Kudo{'s' if n != 1 else ''}",
        [f"{d['username']} left kudos on {d['title']}" for d in new_kudos],
    )


async def _send_ao3_kudos_telegram(new_kudos: list[dict]) -> None:
    """Send Telegram notification for new kudos."""
    settings = config.get_settings()
    n = len(new_kudos)
    await notifications.maybe_send_telegram_summary(
        settings,
        f"<b>📖 AO3: {n} New Kudo{'s' if n != 1 else ''}</b>",
        [f"{_esc(d['username'])} → {_esc(d['title'])}" for d in new_kudos],
        log_label="AO3 kudos",
    )


def _get_or_create_client(settings: dict, ao3_user: str, ao3_pass: str,
                          ao3_target: str, ao3_cookie: str) -> AO3Client:
    """Return the persistent AO3Client, re-pointed at the account's creds."""
    global _ao3_client

    if _ao3_client is None:
        from polling.cf_proxy import proxy_kwargs
        _ao3_client = AO3Client(
            username=ao3_user,
            password=ao3_pass,
            target_user=ao3_target,
            session_cookie=ao3_cookie,
            **proxy_kwargs(settings, "ao3"),
        )
    else:
        _ao3_client.update_credentials(ao3_user, ao3_pass, ao3_target,
                                        session_cookie=ao3_cookie)

    return _ao3_client


async def run_ao3_poll_cycle(account_id: int | None = None, force_full: bool = False) -> dict:
    """Execute one complete AO3 poll cycle for a single account.

    Steps:
      1. Login and validate session
      2. Discover all works for the target user
      3. Fetch details for each work
      4. Upsert works and record snapshots
      5. Track kudos users
    """
    global _ao3_poll_running

    from database import accounts as accounts_db
    _ac = get_connection()
    try:
        if account_id is None:
            account_id = accounts_db.get_default_account_id(_ac, "ao3", create=True)
        account_row = accounts_db.get_account(_ac, account_id)
    finally:
        _ac.close()
    is_default = bool(account_row["is_default"]) if account_row else True
    is_first = account_id not in _ao3_first_poll_done

    # Backoff-state gate (2.22.6): if a previous request hit a 429 with
    # a Retry-After window that's still active, AO3 will just 429 us
    # again — and each fresh request inside the window can extend the
    # punishment. Skip the cycle entirely until the window expires. The
    # 240-min orchestrator interval means we'll catch up on the next
    # natural cycle without escalating the throttle.
    from clients.ao3.client import get_backoff_until_ts
    backoff_until = get_backoff_until_ts()
    if backoff_until:
        remaining = backoff_until - time.time()
        if remaining > 0:
            logger.warning(
                "AO3 poll skipped — %d s remain in observed throttle window",
                int(remaining),
            )
            return {
                "submissions_found": 0,
                "snapshots_inserted": 0,
                "new_kudos_found": 0,
                "skipped_reason": f"throttled, {int(remaining)}s remaining",
            }

    if not _ao3_poll_lock.acquire(blocking=False):
        logger.warning("AO3 poll already running -- skipping (account %s)", account_id)
        return {}
    _ao3_poll_running = True
    _update_ao3_progress("starting", message="Initialising AO3 poll cycle...")

    conn = None
    log_id = None
    start_time = time.time()

    stats = {
        "submissions_found": 0,
        "snapshots_inserted": 0,
        "new_kudos_found": 0,
    }

    settings = config.get_settings()
    creds = config.resolve_account_credentials("ao3", account_id, is_default, settings)
    client = _get_or_create_client(settings, creds.get("ao3_username", ""),
                                   creds.get("ao3_password", ""), creds.get("ao3_target_user", ""),
                                   creds.get("ao3_session_cookie", ""))

    try:
        conn = get_connection()
        log_id = ao3_queries.start_ao3_poll_log(conn, account_id)
        # Step 1: Authenticate
        # Use ensure_logged_in() not validate_session() — the latter does an
        # extra `/users/{name}` probe to confirm the cookie is alive, which
        # AO3 rate-limits per-IP at 429 with ~120s backoff (the same throttle
        # that motivates cookie-only auth in the first place). The cycle's
        # actual work (works-list scrape + per-work details) will fail
        # loudly if the cookie has expired, so the probe adds only delay,
        # not safety. ensure_logged_in() trusts the cookie without fetching.
        _update_ao3_progress("searching", message="Authenticating with AO3...")
        if not await client.ensure_logged_in():
            raise ValueError("AO3 login failed -- check credentials or AO3 may be blocking (see logs for HTTP status)")

        # Step 2: Discover works
        _update_ao3_progress("searching", message="Fetching works list...")
        works = await client.get_all_work_ids()
        work_ids = [w["work_id"] for w in works]
        stats["submissions_found"] = len(work_ids)
        logger.info("AO3: Found %d works", len(work_ids))

        if not work_ids:
            _update_ao3_progress("complete", message="No AO3 works found.")
            ao3_queries.finish_ao3_poll_log(conn, log_id, "success",
                                             duration_seconds=time.time() - start_time, **stats)
            conn.commit()
            return stats

        # Step 3: Fetch details
        _update_ao3_progress("fetching_details",
                             message=f"Fetching details for {len(work_ids)} works...")
        details = await client.get_work_details_batch(work_ids)
        logger.info("AO3: Fetched details for %d works", len(details))

        # Step 4: Upsert + snapshot
        poll_timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        new_kudos_details: list[dict] = []

        for idx, detail in enumerate(details, 1):
            _update_ao3_progress("processing", current=idx, total=len(details),
                                 message=f"Processing work {idx}/{len(details)}...")
            try:
                wid = detail["work_id"]
                views = detail.get("views", 0)
                faves = detail.get("favorites_count", 0)
                comments = detail.get("comments_count", 0)
                bookmarks = detail.get("bookmarks_count", 0)

                # Defence in depth against transient scrape failures writing a
                # 0-view snapshot. AO3 "hits" are cumulative and never drop, so
                # a freshly-scraped 0 when the DB already holds a non-zero count
                # means the fetch/parse failed (challenge page, partial HTML),
                # not that the work reset. Persisting it corrupts the baseline
                # and makes the next good poll look like a +N,000 spike in the
                # digest/milestone deltas. Skip the work this cycle; the next
                # one re-reads the real value. (The client now raises on hard
                # fetch failures, but a 200-with-garbage page could still slip a
                # partial 0 through to here.)
                if views == 0:
                    existing = ao3_queries.get_ao3_submission(conn, wid)
                    if existing and (existing.get("views") or 0) > 0:
                        logger.warning(
                            "AO3: skipping work %s — scraped 0 views but DB has "
                            "%d (transient fetch/parse failure, not a reset)",
                            wid, existing["views"],
                        )
                        continue

                ao3_queries.upsert_ao3_submission(conn, detail, account_id)
                ao3_queries.insert_ao3_snapshot(conn, account_id, wid, views, faves, comments,
                                                bookmarks, polled_at=poll_timestamp)
                stats["snapshots_inserted"] += 1
                # Commit before the kudos fetch below: AO3 paces requests at
                # 12s intervals, so holding the implicit write transaction
                # across that await starves every other poller's writes past
                # the 30s busy_timeout -> "database is locked".
                conn.commit()

                # Step 5: Track kudos users
                try:
                    kudos_users = await client.get_kudos_users(wid)
                    # Batch insert: get existing usernames first to identify new ones
                    existing_usernames = {r["username"] for r in ao3_queries.get_ao3_kudos_users(conn, wid)}
                    new_count = ao3_queries.upsert_ao3_kudos_users_batch(conn, account_id, wid, kudos_users)
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
                logger.warning("Error processing AO3 work %s: %s",
                               detail.get("work_id"), e, exc_info=True)

        conn.commit()

        # ── Notifications (kudos) ────────────────────────────
        if is_first:
            logger.info("First AO3 poll for account %s -- suppressing %d kudos notifications",
                        account_id, len(new_kudos_details))
        else:
            if new_kudos_details:
                try:
                    _send_ao3_kudos_notifications(new_kudos_details)
                except Exception as ne:
                    logger.warning("Failed to send AO3 kudos notifications: %s", ne, exc_info=True)
                try:
                    await _send_ao3_kudos_telegram(new_kudos_details)
                except Exception as te:
                    logger.warning("Failed to send AO3 kudos Telegram: %s", te, exc_info=True)

        # Finalise
        duration = time.time() - start_time
        _update_ao3_progress("complete", current=len(details), total=len(details),
                             message=f"Done -- {stats['submissions_found']} works, "
                                     f"{stats['new_kudos_found']} new kudos in {duration:.1f}s")
        ao3_queries.finish_ao3_poll_log(conn, log_id, "success",
                                         duration_seconds=duration, **stats)
        logger.info("AO3 poll complete in %.1fs -- %d works, %d snapshots, %d new kudos",
                     duration, stats["submissions_found"], stats["snapshots_inserted"],
                     stats["new_kudos_found"])

        # ── Telegram notifications ────────────────────────────
        if not is_first:
            from polling.telegram import send_poll_summary, check_milestones_batch, check_goals
            try:
                await send_poll_summary("ao3", stats, duration)
            except Exception as te:
                logger.warning("Failed to send AO3 Telegram summary: %s", te, exc_info=True)
            try:
                await check_milestones_batch("ao3", "ao3_snapshots", "ao3_submissions", account_id)
            except Exception as me:
                logger.warning("Failed to check AO3 milestones: %s", me, exc_info=True)
            try:
                await check_goals()
            except Exception as ge:
                logger.warning("Failed to check goals: %s", ge, exc_info=True)

        return stats

    except Exception as e:
        duration = time.time() - start_time
        _update_ao3_progress("error", message=describe_error(e))
        logger.error("AO3 poll failed: %s", describe_error(e), exc_info=True)
        if conn and log_id:
            ao3_queries.finish_ao3_poll_log(conn, log_id, "error",
                                             error_message=describe_error(e),
                                             duration_seconds=duration, **stats)
            conn.commit()
        from polling.telegram import send_poll_error
        try:
            await send_poll_error("ao3", e)
        except Exception:
            logger.debug("Error alert send failed", exc_info=True)
        raise
    finally:
        _ao3_first_poll_done.add(account_id)
        _ao3_poll_running = False
        _ao3_poll_lock.release()
        if conn:
            conn.close()
