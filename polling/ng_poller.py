"""Newgrounds (ng) poll cycle orchestration — MEDIAPLATS §5 (4.23.0).

Cookie session, HTML scraped (clients/ng/client.py). Polls the operator's own
audio and movie listings and each item's stats block (Listens / Views, Faves,
Downloads, Votes, Score) into the standard triple; the profile page's FANS
count is the follower series. Mirrors polling/sc_poller.py; gently paced —
one item page at a time, a short pause between, because there is no API and
the site fronts fetches with a bot guard (❓ probe from the VM first).
"""
from __future__ import annotations

import asyncio
import atexit
import logging
import threading
import time
from datetime import datetime, timezone
from html import escape as _esc

import config
from clients.ng.client import NgClient
from database.db import get_connection
from database import ng_queries
from polling import notifications
from polling.followers import capture_followers
from polling.notifications import describe_error

logger = logging.getLogger(__name__)

ng_poll_progress = {"active": False, "phase": "idle", "current": 0, "total": 0, "message": ""}

_ng_poll_lock = threading.Lock()
_ng_first_poll_done: set[int] = set()
_ng_client: NgClient | None = None

TOKEN_FIELDS = ("sc_access_token", "sc_refresh_token", "sc_token_expires_at")


def _cleanup_ng_client():
    if _ng_client is not None:
        import asyncio
        try:
            asyncio.get_event_loop().run_until_complete(_ng_client.close())
        except Exception:
            logger.debug("ng client cleanup failed", exc_info=True)


atexit.register(_cleanup_ng_client)


def _update_ng_progress(phase: str, current: int = 0, total: int = 0, message: str = ""):
    ng_poll_progress.update(active=phase not in ("idle", "complete", "error"),
                            phase=phase, current=current, total=total, message=message)


def _send_ng_notifications(new_details: list[dict]) -> None:
    settings = config.get_settings()
    n = len(new_details)
    notifications.maybe_show_toast(
        settings, "sc_notifications_enabled",
        f"Newgrounds: {n} Track{'s' if n != 1 else ''} Updated",
        [f"{d['title'][:50]} gained activity" for d in new_details])


async def _send_ng_telegram(new_details: list[dict]) -> None:
    settings = config.get_settings()
    n = len(new_details)
    await notifications.maybe_send_telegram_summary(
        settings,
        f"<b>\U0001f3b5 Newgrounds: {n} Track{'s' if n != 1 else ''} Updated</b>",
        [_esc(d['title'][:50]) for d in new_details], log_label="Newgrounds")


def client_from_creds(creds: dict) -> NgClient:
    """A fresh client pointed at one account's cookie + username (the poster uses this too)."""
    return NgClient(username=creds.get("ng_username", ""), cookie=creds.get("ng_cookie", ""))


def _get_or_create_client(creds: dict) -> NgClient:
    """Return the persistent NgClient, re-pointed at the account's credentials."""
    global _ng_client
    if _ng_client is None or _ng_client.cookie != (creds.get("ng_cookie") or "").strip()             or _ng_client.username != (creds.get("ng_username") or "").strip():
        _ng_client = client_from_creds(creds)
    return _ng_client


ITEM_PAUSE_S = 1.5      # between item pages — no API, so be a polite browser


async def run_ng_poll_cycle(account_id: int | None = None, force_full: bool = False) -> dict:
    """Execute one complete Newgrounds poll cycle for a single account."""
    from database import accounts as accounts_db
    _ac = get_connection()
    try:
        if account_id is None:
            account_id = accounts_db.get_default_account_id(_ac, "ng", create=True)
        account_row = accounts_db.get_account(_ac, account_id)
    finally:
        _ac.close()
    is_default = bool(account_row["is_default"]) if account_row else True
    is_first = account_id not in _ng_first_poll_done

    if not _ng_poll_lock.acquire(blocking=False):
        logger.warning("ng poll already running -- skipping (account %s)", account_id)
        return {}
    _update_ng_progress("starting", message="Initialising Newgrounds poll cycle...")

    conn = None
    log_id = None
    start_time = time.time()
    stats = {"submissions_found": 0, "snapshots_inserted": 0}

    settings = config.get_settings()
    creds = config.resolve_account_credentials("ng", account_id, is_default, settings)
    client = _get_or_create_client(creds)

    try:
        conn = get_connection()
        log_id = ng_queries.start_ng_poll_log(conn, account_id)

        _update_ng_progress("searching", message="Checking the Newgrounds session...")
        session = await client.validate_session()
        if not session.get("ok"):
            raise ValueError(f"Newgrounds session check failed -- {session.get('detail') or 'log in again in Settings'}")

        _update_ng_progress("searching", message="Fetching your audio and movie listings...")
        tracks = await client.get_all_items("audio") + await client.get_all_items("movie")
        stats["submissions_found"] = len(tracks)

        if not tracks:
            _update_ng_progress("complete", message="No Newgrounds submissions found.")
            await capture_followers(client, account_id, conn)
            ng_queries.finish_ng_poll_log(conn, log_id, "success",
                                          duration_seconds=time.time() - start_time, **stats)
            conn.commit()
            return stats

        new_activity_details: list[dict] = []
        poll_ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        for idx, raw in enumerate(tracks, 1):
            _update_ng_progress("processing", current=idx, total=len(tracks),
                                message=f"Processing item {idx}/{len(tracks)}...")
            try:
                sid = raw["submission_id"]
                detail = await client.get_item(sid, raw.get("portal", "audio"))
                if not detail:
                    logger.info("ng: item %s page had no stats block -- skipped this cycle", sid)
                    continue
                if idx < len(tracks):
                    await asyncio.sleep(ITEM_PAUSE_S)
                views, faves, comments = detail["views"], detail["favorites_count"], detail["comments_count"]
                prev = ng_queries.get_ng_submission(conn, sid)
                if prev and (faves > prev.get("favorites_count", 0)
                             or comments > prev.get("comments_count", 0)):
                    new_activity_details.append({"title": detail.get("title", "")})
                ng_queries.upsert_ng_submission(conn, detail, account_id)
                ng_queries.insert_ng_snapshot(conn, account_id, sid, views, faves, comments, polled_at=poll_ts,
                                              score=detail.get("score", 0.0), votes=detail.get("votes", 0),
                                              downloads_count=detail.get("downloads_count", 0))
                stats["snapshots_inserted"] += 1
            except Exception as e:
                logger.warning("Error processing ng item %s: %s", str(raw.get("submission_id", ""))[:50], e, exc_info=True)
        conn.commit()

        await capture_followers(client, account_id, conn)

        if is_first:
            logger.info("First ng poll for account %s -- suppressing %d activity notifications",
                        account_id, len(new_activity_details))
        else:
            try:
                _send_ng_notifications(new_activity_details)
            except Exception as ne:
                logger.warning("Failed to send sc notifications: %s", ne, exc_info=True)
            try:
                await _send_ng_telegram(new_activity_details)
            except Exception as te:
                logger.warning("Failed to send ng Telegram notification: %s", te, exc_info=True)

        duration = time.time() - start_time
        _update_ng_progress("complete", current=len(tracks), total=len(tracks),
                            message=f"Done -- {stats['submissions_found']} submissions in {duration:.1f}s")
        ng_queries.finish_ng_poll_log(conn, log_id, "success", duration_seconds=duration, **stats)
        logger.info("ng poll complete in %.1fs -- %d submissions, %d snapshots",
                    duration, stats["submissions_found"], stats["snapshots_inserted"])

        if not is_first:
            from polling.telegram import send_poll_summary, check_milestones_batch, check_goals
            for coro, label in ((send_poll_summary("ng", stats, duration), "summary"),
                                (check_milestones_batch("ng", "sc_snapshots", "sc_submissions", account_id), "milestones"),
                                (check_goals(), "goals")):
                try:
                    await coro
                except Exception as te:
                    logger.warning("ng Telegram %s failed: %s", label, te, exc_info=True)

        return stats

    except Exception as e:
        duration = time.time() - start_time
        _update_ng_progress("error", message=describe_error(e))
        logger.error("ng poll failed: %s", describe_error(e), exc_info=True)
        if conn and log_id:
            ng_queries.finish_ng_poll_log(conn, log_id, "error", error_message=describe_error(e),
                                          duration_seconds=duration, **stats)
            conn.commit()
        from polling.telegram import send_poll_error
        try:
            await send_poll_error("ng", e)
        except Exception:
            logger.debug("Error alert send failed", exc_info=True)
        raise
    finally:
        _ng_first_poll_done.add(account_id)
        _ng_poll_lock.release()
        if conn:
            conn.close()
