"""DeviantArt (DA) poll cycle orchestration.

Mirrors the AO3 poller pattern (simpler than FA, no comments scraping,
no watchers). DeviantArt uses cookie-based auth with Eclipse _napi endpoints.

Key differences from other pollers:
  - Uses DAClient with the official OAuth2 API (client_id/client_secret); the
    legacy cookie path is a fallback only (2.47.0).
  - Settings keys: da_client_id, da_client_secret, da_target_user (da_cookie is
    only consulted for the legacy fallback path).
  - Stats: submissions_found, snapshots_inserted (no kudos/comments tracking)
  - Notifications: "DA:" prefix
  - Downloads metric tracked in addition to views/favorites/comments
  - No CF proxy: the official API is not IP-walled from datacenter IPs.
"""

from __future__ import annotations
import atexit
import logging
import threading
import time
from datetime import datetime, timezone
from html import escape as _esc

from polling import notifications

import config
from clients.da.client import DAClient
from database.db import get_connection
from polling.notifications import describe_error
from polling.followers import capture_followers
from database import da_queries

logger = logging.getLogger(__name__)

# -- Progress tracking -------------------------------------------------
da_poll_progress = {
    "active": False,
    "phase": "idle",
    "current": 0,
    "total": 0,
    "message": "",
}

_da_poll_running = False
_da_poll_lock = threading.Lock()
# Per-account first-poll suppression (single lock serialises DA polls).
_da_first_poll_done: set[int] = set()

# Persistent client — reused across poll cycles
_da_client: DAClient | None = None


def _cleanup_da_client():
    if _da_client is not None:
        import asyncio
        try:
            asyncio.get_event_loop().run_until_complete(_da_client.close())
        except Exception:
            logger.debug("Error alert send failed", exc_info=True)


atexit.register(_cleanup_da_client)


def _update_da_progress(phase: str, current: int = 0, total: int = 0, message: str = ""):
    da_poll_progress["active"] = phase not in ("idle", "complete", "error")
    da_poll_progress["phase"] = phase
    da_poll_progress["current"] = current
    da_poll_progress["total"] = total
    da_poll_progress["message"] = message


def _send_da_notifications(new_details: list[dict]) -> None:
    """Send Windows toast notifications for DA activity."""
    settings = config.get_settings()
    n = len(new_details)
    notifications.maybe_show_toast(
        settings,
        "da_notifications_enabled",
        f"DA: {n} Deviation{'s' if n != 1 else ''} Updated",
        [f"{d['title']} gained activity" for d in new_details],
    )


async def _send_da_telegram(new_details: list[dict]) -> None:
    """Send Telegram notification for DA activity."""
    settings = config.get_settings()
    n = len(new_details)
    await notifications.maybe_send_telegram_summary(
        settings,
        f"<b>🎨 DA: {n} Deviation{'s' if n != 1 else ''} Updated</b>",
        [_esc(d['title']) for d in new_details],
        log_label="DA",
    )


def _get_or_create_client(settings: dict, client_id: str, client_secret: str,
                          target_user: str, cookie: str = "") -> DAClient:
    """Return the persistent DAClient, re-pointed at the given account's creds.

    DA accounts poll sequentially (single lock), so one persistent client that
    gets its credentials updated each cycle is sufficient. Polling uses the
    official OAuth2 API (client_id/client_secret) and needs no CF proxy; the
    cookie is passed through only for the legacy fallback path.
    """
    global _da_client

    if _da_client is None:
        _da_client = DAClient(
            client_id=client_id,
            client_secret=client_secret,
            target_user=target_user,
            cookie_value=cookie,
        )
    else:
        _da_client.update_credentials(
            client_id=client_id,
            client_secret=client_secret,
            target_user=target_user,
            cookie_value=cookie,
        )

    return _da_client


async def run_da_poll_cycle(account_id: int | None = None, force_full: bool = False) -> dict:
    """Execute one complete DA poll cycle for a single account.

    Steps:
      1. Validate cookies by accessing gallery page
      2. Discover all deviations for the target user
      3. Fetch details for each deviation
      4. Upsert deviations and record snapshots
    """
    global _da_poll_running

    from database import accounts as accounts_db
    _ac = get_connection()
    try:
        if account_id is None:
            account_id = accounts_db.get_default_account_id(_ac, "da", create=True)
        account_row = accounts_db.get_account(_ac, account_id)
    finally:
        _ac.close()
    is_default = bool(account_row["is_default"]) if account_row else True
    is_first = account_id not in _da_first_poll_done

    if not _da_poll_lock.acquire(blocking=False):
        logger.warning("DA poll already running -- skipping (account %s)", account_id)
        return {}
    _da_poll_running = True
    _update_da_progress("starting", message="Initialising DA poll cycle...")

    conn = None
    log_id = None
    start_time = time.time()

    stats = {
        "submissions_found": 0,
        "snapshots_inserted": 0,
    }

    settings = config.get_settings()
    creds = config.resolve_account_credentials("da", account_id, is_default, settings)
    client = _get_or_create_client(
        settings,
        creds.get("da_client_id", ""),
        creds.get("da_client_secret", ""),
        creds.get("da_target_user", ""),
        creds.get("da_cookie", ""),
    )

    try:
        conn = get_connection()
        log_id = da_queries.start_da_poll_log(conn, account_id)
        # Step 1: Validate credentials (OAuth token + gallery reachable)
        _update_da_progress("searching", message="Validating DA credentials...")
        if not (creds.get("da_client_id") and creds.get("da_client_secret")) \
                and not creds.get("da_cookie"):
            raise ValueError("DeviantArt not configured -- set client_id/client_secret "
                             "(or a cookie for the legacy path)")
        valid = await client.validate_credentials()
        if not valid:
            raise ValueError("DA credential validation failed -- check client_id/client_secret "
                             "and the target username")

        # Step 2: Discover deviations
        _update_da_progress("searching", message="Fetching deviations list...")
        deviations = await client.get_all_deviation_ids()
        deviation_ids = [d["deviation_id"] for d in deviations]
        stats["submissions_found"] = len(deviation_ids)
        logger.info("DA: Found %d deviations", len(deviation_ids))

        if not deviation_ids:
            _update_da_progress("complete", message="No DA deviations found.")
            da_queries.finish_da_poll_log(conn, log_id, "success",
                                          duration_seconds=time.time() - start_time, **stats)
            conn.commit()
            return stats

        # Step 3: Fetch details
        _update_da_progress("fetching_details",
                            message=f"Fetching details for {len(deviation_ids)} deviations...")
        details = await client.get_deviation_details_batch(deviation_ids)
        logger.info("DA: Fetched details for %d deviations", len(details))

        # Step 4: Upsert + snapshot
        new_activity_details: list[dict] = []
        poll_timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

        for idx, detail in enumerate(details, 1):
            _update_da_progress("processing", current=idx, total=len(details),
                                message=f"Processing deviation {idx}/{len(details)}...")
            try:
                dev_id = detail["deviation_id"]
                views = detail.get("views", 0)
                faves = detail.get("favorites_count", 0)
                comments = detail.get("comments_count", 0)
                downloads = detail.get("downloads", 0)

                # Check for stat increases to drive notifications.
                prev = da_queries.get_da_submission(conn, dev_id)
                if prev and (faves > prev.get("favorites_count", 0)
                             or comments > prev.get("comments_count", 0)):
                    new_activity_details.append({"title": detail.get("title", "")})

                da_queries.upsert_da_submission(conn, detail, account_id)
                da_queries.insert_da_snapshot(conn, account_id, dev_id, views, faves, comments,
                                             downloads, polled_at=poll_timestamp)
                stats["snapshots_inserted"] += 1

            except Exception as e:
                logger.warning("Error processing DA deviation %s: %s",
                               detail.get("deviation_id"), e, exc_info=True)

        conn.commit()

        # ── Inbox capture (gap G3 Stage A1) ───────────────────
        # Official /comments/deviation/{uuid} per behind-looking deviation.
        # Only OAuth-path details carry the uuid (legacy cookie path doesn't —
        # those simply skip). App token; mature deviations may return empty.
        try:
            from polling.inbox_capture import capture as _inbox_capture

            _uuid_by_id = {str(d.get("deviation_id")): (d.get("uuid", ""), d.get("link", ""))
                           for d in details}

            async def _fetch_da_comments(c):
                uuid, link = _uuid_by_id.get(str(c["submission_id"]), ("", ""))
                if not uuid:
                    return []
                return await client.get_deviation_comments(uuid, link)

            await _inbox_capture(
                conn, "da",
                [{"submission_id": str(d.get("deviation_id", "")),
                  "fresh_count": d.get("comments_count", 0) or 0,
                  "title": d.get("title", "")} for d in details if d.get("uuid")],
                _fetch_da_comments, account_id=account_id,
                own_author=getattr(client, "target_user", "") or "")
        except Exception as ce:  # noqa: BLE001 — capture never fails the poll
            logger.warning("DA inbox capture failed: %s", ce)

        # ── Notifications ─────────────────────────────────────
        if is_first:
            logger.info("First DA poll for account %s -- suppressing %d activity notifications",
                        account_id, len(new_activity_details))
        else:
            try:
                _send_da_notifications(new_activity_details)
            except Exception as ne:
                logger.warning("Failed to send DA notifications: %s", ne, exc_info=True)
            try:
                await _send_da_telegram(new_activity_details)
            except Exception as te:
                logger.warning("Failed to send DA Telegram notification: %s", te, exc_info=True)

        # Finalise
        # Follower count: reuse the authed client to snapshot the account's
        # follower total (network fetch first, then a short DB write — no lock
        # held across the await). Best-effort; never fails the cycle.
        await capture_followers(client, account_id, conn)

        duration = time.time() - start_time
        _update_da_progress("complete", current=len(details), total=len(details),
                            message=f"Done -- {stats['submissions_found']} deviations "
                                    f"in {duration:.1f}s")
        da_queries.finish_da_poll_log(conn, log_id, "success",
                                      duration_seconds=duration, **stats)
        logger.info("DA poll complete in %.1fs -- %d deviations, %d snapshots",
                     duration, stats["submissions_found"], stats["snapshots_inserted"])

        # ── Telegram notifications ────────────────────────────
        if not is_first:
            from polling.telegram import send_poll_summary, check_milestones_batch, check_goals
            try:
                await send_poll_summary("da", stats, duration)
            except Exception as te:
                logger.warning("Failed to send DA Telegram summary: %s", te, exc_info=True)
            try:
                await check_milestones_batch("da", "da_snapshots", "da_submissions", account_id)
            except Exception as me:
                logger.warning("Failed to check DA milestones: %s", me, exc_info=True)
            try:
                await check_goals()
            except Exception as ge:
                logger.warning("Failed to check goals: %s", ge, exc_info=True)

        return stats

    except Exception as e:
        duration = time.time() - start_time
        _update_da_progress("error", message=describe_error(e))
        logger.error("DA poll failed: %s", describe_error(e), exc_info=True)
        if conn and log_id:
            da_queries.finish_da_poll_log(conn, log_id, "error",
                                          error_message=describe_error(e),
                                          duration_seconds=duration, **stats)
            conn.commit()
        from polling.telegram import send_poll_error
        try:
            await send_poll_error("da", e)
        except Exception:
            logger.debug("Error alert send failed", exc_info=True)
        raise
    finally:
        _da_first_poll_done.add(account_id)
        _da_poll_running = False
        _da_poll_lock.release()
        if conn:
            conn.close()
