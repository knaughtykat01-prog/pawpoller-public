"""X/Twitter (TW) poll cycle orchestration.

Uses internal GraphQL endpoints with cookie-based auth, the same
approach as the DeviantArt integration.

Key differences from other pollers:
  - Uses TWClient with auth_token, ct0, and target_user
  - Settings keys: tw_auth_token, tw_ct0, tw_target_user
  - Stats: views, likes, retweets, replies, quotes, bookmarks (6 metrics)
  - Notifications: "TW:" prefix, bird emoji
  - Higher rate limit delay (2.0s) due to X's aggressive rate limiting
"""

from __future__ import annotations
import atexit
import logging
import threading
import time
from datetime import datetime, timezone
from html import escape as _esc

import config
from clients.tw.client import TWClient
from database.db import get_connection
from polling.notifications import describe_error
from polling.followers import capture_followers
from database import tw_queries
from polling import notifications

logger = logging.getLogger(__name__)

# -- Progress tracking --------------------------------------------------------
tw_poll_progress = {
    "active": False,
    "phase": "idle",
    "current": 0,
    "total": 0,
    "message": "",
}

_tw_poll_running = False
_tw_poll_lock = threading.Lock()
_tw_first_poll_done: set[int] = set()

# Persistent client — reused across poll cycles
_tw_client: TWClient | None = None


def _cleanup_tw_client():
    if _tw_client is not None:
        import asyncio
        try:
            asyncio.get_event_loop().run_until_complete(_tw_client.close())
        except Exception:
            logger.debug("Error alert send failed", exc_info=True)


atexit.register(_cleanup_tw_client)


def _update_tw_progress(phase: str, current: int = 0, total: int = 0, message: str = ""):
    tw_poll_progress["active"] = phase not in ("idle", "complete", "error")
    tw_poll_progress["phase"] = phase
    tw_poll_progress["current"] = current
    tw_poll_progress["total"] = total
    tw_poll_progress["message"] = message


def _send_tw_notifications(new_details: list[dict]) -> None:
    """Send Windows toast notifications for X/Twitter activity."""
    settings = config.get_settings()
    n = len(new_details)
    notifications.maybe_show_toast(
        settings,
        "tw_notifications_enabled",
        f"TW: {n} Tweet{'s' if n != 1 else ''} Updated",
        [f"{d['title'][:50]} gained activity" for d in new_details],
    )


async def _send_tw_telegram(new_details: list[dict]) -> None:
    """Send Telegram notification for X/Twitter activity."""
    settings = config.get_settings()
    n = len(new_details)
    await notifications.maybe_send_telegram_summary(
        settings,
        f"<b>\U0001f426 TW: {n} Tweet{'s' if n != 1 else ''} Updated</b>",
        [_esc(d['title'][:50]) for d in new_details],
        log_label="TW",
    )


def _get_or_create_client(settings: dict, tw_auth_token: str, tw_ct0: str, tw_target: str) -> TWClient:
    """Return the persistent TWClient, re-pointed at the account's credentials."""
    global _tw_client

    if _tw_client is None:
        from polling.cf_proxy import proxy_kwargs
        _tw_client = TWClient(
            auth_token=tw_auth_token,
            ct0=tw_ct0,
            target_user=tw_target,
            **proxy_kwargs(settings, "tw"),
        )
    else:
        _tw_client.update_credentials(tw_auth_token, tw_ct0, tw_target)

    return _tw_client


async def run_tw_poll_cycle(account_id: int | None = None, force_full: bool = False) -> dict:
    """Execute one complete X/Twitter poll cycle for a single account.

    Steps:
      1. Validate cookies by resolving target user
      2. Discover all tweets for the target user
      3. Fetch details for each tweet
      4. Upsert tweets and record snapshots
    """
    global _tw_poll_running

    from database import accounts as accounts_db
    _ac = get_connection()
    try:
        if account_id is None:
            account_id = accounts_db.get_default_account_id(_ac, "tw", create=True)
        account_row = accounts_db.get_account(_ac, account_id)
    finally:
        _ac.close()
    is_default = bool(account_row["is_default"]) if account_row else True
    is_first = account_id not in _tw_first_poll_done

    if not _tw_poll_lock.acquire(blocking=False):
        logger.warning("TW poll already running -- skipping (account %s)", account_id)
        return {}
    _tw_poll_running = True
    _update_tw_progress("starting", message="Initialising TW poll cycle...")

    conn = None
    log_id = None
    start_time = time.time()

    stats = {
        "submissions_found": 0,
        "snapshots_inserted": 0,
    }

    settings = config.get_settings()
    creds = config.resolve_account_credentials("tw", account_id, is_default, settings)
    client = _get_or_create_client(settings, creds.get("tw_auth_token", ""),
                                   creds.get("tw_ct0", ""), creds.get("tw_target_user", ""))
    client.throttled = False   # reset per cycle; set True by the client on a 429

    try:
        conn = get_connection()
        log_id = tw_queries.start_tw_poll_log(conn, account_id)

        # Step 1: Validate credentials for the active backend. The official X API
        # backend polls with a Bearer token and needs NO cookies, so only require
        # auth_token/ct0 when the official backend isn't configured.
        _update_tw_progress("searching", message="Validating X/Twitter credentials...")
        from clients.tw import official_api as _tw_official
        _official_active = _tw_official.is_enabled(settings)
        if not _official_active and (not client.auth_token or not client.ct0):
            raise ValueError("X/Twitter credentials missing — set auth_token and ct0 cookies, "
                             "or an X API Bearer token, in Settings")
        valid = await client.validate_cookies()
        if not valid:
            raise ValueError("X/Twitter credential validation failed -- update the auth_token/ct0 "
                             "cookies or the X API Bearer token in Settings")

        # Step 2: Fetch tweets. Stats come straight from the UserTweets timeline
        # (the per-tweet TweetResultByRestId endpoint 404s), so there's no second
        # detail pass — get_all_tweets() returns full detail dicts.
        _update_tw_progress("searching", message="Fetching tweets...")
        details = await client.get_all_tweets()
        stats["submissions_found"] = len(details)
        logger.info("TW: Found %d tweets", len(details))

        if not details:
            # No tweets could mean genuinely-none OR the timeline was rate-limited
            # away — distinguish so a throttled cycle isn't a silent "success".
            _throttled = getattr(client, "throttled", False)
            _status = "partial" if _throttled else "success"
            _msg = "Rate-limited (429) — tweets may be incomplete" if _throttled else None
            _update_tw_progress("complete", message="Rate-limited — data may be incomplete" if _throttled else "No tweets found.")
            tw_queries.finish_tw_poll_log(conn, log_id, _status, error_message=_msg,
                                          duration_seconds=time.time() - start_time, **stats)
            conn.commit()
            return stats

        # Step 4: Upsert + snapshot
        new_activity_details: list[dict] = []
        poll_timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

        for idx, detail in enumerate(details, 1):
            _update_tw_progress("processing", current=idx, total=len(details),
                                message=f"Processing tweet {idx}/{len(details)}...")
            try:
                tweet_id = detail["tweet_id"]
                views = detail.get("views", 0)
                likes = detail.get("likes", 0)
                retweets = detail.get("retweets", 0)
                replies_count = detail.get("replies", 0)
                quotes = detail.get("quotes", 0)
                bookmarks = detail.get("bookmarks", 0)

                # Check for stat increases to drive notifications
                prev = tw_queries.get_tw_submission(conn, tweet_id)
                if prev and (likes > prev.get("likes", 0)
                             or retweets > prev.get("retweets", 0)):
                    new_activity_details.append({"title": detail.get("title", "")})

                tw_queries.upsert_tw_submission(conn, detail, account_id)
                tw_queries.insert_tw_snapshot(conn, account_id, tweet_id, views, likes,
                                              retweets, replies_count, quotes,
                                              bookmarks, polled_at=poll_timestamp)
                stats["snapshots_inserted"] += 1

            except Exception as e:
                logger.warning("Error processing TW tweet %s: %s",
                               detail.get("tweet_id"), e, exc_info=True)

        conn.commit()

        # ── Notifications ─────────────────────────────────────
        if is_first:
            logger.info("First TW poll for account %s -- suppressing %d activity notifications",
                        account_id, len(new_activity_details))
        else:
            try:
                _send_tw_notifications(new_activity_details)
            except Exception as ne:
                logger.warning("Failed to send TW notifications: %s", ne, exc_info=True)
            try:
                await _send_tw_telegram(new_activity_details)
            except Exception as te:
                logger.warning("Failed to send TW Telegram notification: %s", te, exc_info=True)

        # Finalise
        # Follower count: reuse the authed client to snapshot the account's
        # follower total (network fetch first, then a short DB write — no lock
        # held across the await). Best-effort; never fails the cycle.
        await capture_followers(client, account_id, conn)

        duration = time.time() - start_time
        # A 429 mid-cycle → 'partial' (some tweets may be missing), not clean 'success'.
        _throttled = getattr(client, "throttled", False)
        _status = "partial" if _throttled else "success"
        _msg = "Rate-limited (429) — some tweets may be missing" if _throttled else None
        _update_tw_progress("complete", current=len(details), total=len(details),
                            message=(f"Throttled -- {stats['submissions_found']} tweets (may be incomplete)"
                                     if _throttled else f"Done -- {stats['submissions_found']} tweets in {duration:.1f}s"))
        tw_queries.finish_tw_poll_log(conn, log_id, _status, error_message=_msg,
                                      duration_seconds=duration, **stats)
        logger.info("TW poll %s in %.1fs -- %d tweets, %d snapshots",
                     _status, duration, stats["submissions_found"], stats["snapshots_inserted"])

        # -- Telegram notifications ----------------------------------------
        if not is_first:
            from polling.telegram import send_poll_summary, check_milestones_batch, check_goals
            try:
                await send_poll_summary("tw", stats, duration)
            except Exception as te:
                logger.warning("Failed to send TW Telegram summary: %s", te, exc_info=True)
            try:
                await check_milestones_batch("tw", "tw_snapshots", "tw_submissions", account_id)
            except Exception as me:
                logger.warning("Failed to check TW milestones: %s", me, exc_info=True)
            try:
                await check_goals()
            except Exception as ge:
                logger.warning("Failed to check goals: %s", ge, exc_info=True)

        return stats

    except Exception as e:
        duration = time.time() - start_time
        _update_tw_progress("error", message=describe_error(e))
        logger.error("TW poll failed: %s", describe_error(e), exc_info=True)
        if conn and log_id:
            tw_queries.finish_tw_poll_log(conn, log_id, "error",
                                          error_message=describe_error(e),
                                          duration_seconds=duration, **stats)
            conn.commit()
        from polling.telegram import send_poll_error
        try:
            await send_poll_error("tw", e)
        except Exception:
            logger.debug("Error alert send failed", exc_info=True)
        raise
    finally:
        _tw_first_poll_done.add(account_id)
        _tw_poll_running = False
        _tw_poll_lock.release()
        if conn:
            conn.close()
