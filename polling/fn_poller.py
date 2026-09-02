"""FurryNetwork (fn) poll cycle orchestration.

OAuth2 (email+password → bearer/refresh). Polls the connected user's own
submissions across their FN characters and snapshots views/favorites/comments.
FN exposes a follower count, so this poller captures a follower series too
(unlike e621). Mirrors polling/e621_poller.py.
"""
from __future__ import annotations

import atexit
import logging
import threading
import time
from datetime import datetime, timezone
from html import escape as _esc

import config
from clients.fn.client import FnClient
from database.db import get_connection
from database import fn_queries
from polling import notifications
from polling.followers import capture_followers
from polling.notifications import describe_error

logger = logging.getLogger(__name__)

fn_poll_progress = {"active": False, "phase": "idle", "current": 0, "total": 0, "message": ""}

_fn_poll_lock = threading.Lock()
_fn_first_poll_done: set[int] = set()
_fn_client: FnClient | None = None


def _cleanup_fn_client():
    if _fn_client is not None:
        import asyncio
        try:
            asyncio.get_event_loop().run_until_complete(_fn_client.close())
        except Exception:
            logger.debug("fn client cleanup failed", exc_info=True)


atexit.register(_cleanup_fn_client)


def _update_fn_progress(phase: str, current: int = 0, total: int = 0, message: str = ""):
    fn_poll_progress.update(active=phase not in ("idle", "complete", "error"),
                            phase=phase, current=current, total=total, message=message)


def _send_fn_notifications(new_details: list[dict]) -> None:
    settings = config.get_settings()
    n = len(new_details)
    notifications.maybe_show_toast(
        settings, "fn_notifications_enabled",
        f"FurryNetwork: {n} Submission{'s' if n != 1 else ''} Updated",
        [f"{d['title'][:50]} gained activity" for d in new_details])


async def _send_fn_telegram(new_details: list[dict]) -> None:
    settings = config.get_settings()
    n = len(new_details)
    await notifications.maybe_send_telegram_summary(
        settings,
        f"<b>\U0001f43e FurryNetwork: {n} Submission{'s' if n != 1 else ''} Updated</b>",
        [_esc(d['title'][:50]) for d in new_details], log_label="FurryNetwork")


def _get_or_create_client(creds: dict) -> FnClient:
    """Return the persistent FnClient, re-pointed at the account's credentials."""
    global _fn_client
    if _fn_client is None:
        _fn_client = FnClient(
            username=creds.get("fn_username", ""), password=creds.get("fn_password", ""),
            access_token=creds.get("fn_access_token", ""),
            refresh_token=creds.get("fn_refresh_token", ""))
    else:
        _fn_client.username = creds.get("fn_username", "")
        _fn_client.password = creds.get("fn_password", "")
        if creds.get("fn_refresh_token"):
            _fn_client.refresh_token = creds["fn_refresh_token"]
    return _fn_client


def _persist_tokens(client: FnClient, is_default: bool) -> None:
    """FN rotates the refresh token on every refresh — persist it (and the access
    token) so the next cycle doesn't have to fall back to a password login. Only
    the default account writes the flat keys (multi-account token persistence is
    a later refinement)."""
    if not is_default:
        return
    upd = {}
    if client.refresh_token:
        upd["fn_refresh_token"] = client.refresh_token
    if client.access_token:
        upd["fn_access_token"] = client.access_token
    if upd:
        try:
            config.save_settings(upd)
        except Exception:
            logger.debug("fn token persist failed", exc_info=True)


async def run_fn_poll_cycle(account_id: int | None = None, force_full: bool = False) -> dict:
    """Execute one complete FurryNetwork poll cycle for a single account."""
    from database import accounts as accounts_db
    _ac = get_connection()
    try:
        if account_id is None:
            account_id = accounts_db.get_default_account_id(_ac, "fn", create=True)
        account_row = accounts_db.get_account(_ac, account_id)
    finally:
        _ac.close()
    is_default = bool(account_row["is_default"]) if account_row else True
    is_first = account_id not in _fn_first_poll_done

    if not _fn_poll_lock.acquire(blocking=False):
        logger.warning("fn poll already running -- skipping (account %s)", account_id)
        return {}
    _update_fn_progress("starting", message="Initialising FurryNetwork poll cycle...")

    conn = None
    log_id = None
    start_time = time.time()
    stats = {"submissions_found": 0, "snapshots_inserted": 0}

    settings = config.get_settings()
    creds = config.resolve_account_credentials("fn", account_id, is_default, settings)
    client = _get_or_create_client(creds)

    try:
        conn = get_connection()
        log_id = fn_queries.start_fn_poll_log(conn, account_id)

        _update_fn_progress("searching", message="Authenticating with FurryNetwork...")
        name = await client.validate_session()
        if not name:
            raise ValueError("FurryNetwork auth failed -- check the email + password")
        _persist_tokens(client, is_default)

        _update_fn_progress("searching", message="Fetching submission list...")
        post_items = await client.get_all_post_uris()
        stats["submissions_found"] = len(post_items)

        if not post_items:
            _update_fn_progress("complete", message="No FurryNetwork submissions found.")
            await capture_followers(client, account_id, conn)
            fn_queries.finish_fn_poll_log(conn, log_id, "success",
                                          duration_seconds=time.time() - start_time, **stats)
            conn.commit()
            return stats

        _update_fn_progress("fetching_details",
                            message=f"Parsing details for {len(post_items)} submissions...")
        details = await client.get_post_details_batch(post_items)

        new_activity_details: list[dict] = []
        poll_ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        for idx, detail in enumerate(details, 1):
            _update_fn_progress("processing", current=idx, total=len(details),
                                message=f"Processing submission {idx}/{len(details)}...")
            try:
                uri = detail["post_uri"]
                views = detail.get("views", 0)
                faves = detail.get("favorites_count", 0)
                comments = detail.get("comments_count", 0)
                prev = fn_queries.get_fn_submission(conn, uri)
                if prev and (faves > prev.get("favorites_count", 0)
                             or comments > prev.get("comments_count", 0)):
                    new_activity_details.append({"title": detail.get("title", "")})
                fn_queries.upsert_fn_submission(conn, detail, account_id)
                fn_queries.insert_fn_snapshot(conn, account_id, uri, views, faves,
                                              comments, polled_at=poll_ts)
                stats["snapshots_inserted"] += 1
            except Exception as e:
                logger.warning("Error processing fn submission %s: %s",
                               str(detail.get("post_uri", ""))[:50], e, exc_info=True)
        conn.commit()

        await capture_followers(client, account_id, conn)

        if is_first:
            logger.info("First fn poll for account %s -- suppressing %d activity notifications",
                        account_id, len(new_activity_details))
        else:
            try:
                _send_fn_notifications(new_activity_details)
            except Exception as ne:
                logger.warning("Failed to send fn notifications: %s", ne, exc_info=True)
            try:
                await _send_fn_telegram(new_activity_details)
            except Exception as te:
                logger.warning("Failed to send fn Telegram notification: %s", te, exc_info=True)

        duration = time.time() - start_time
        _update_fn_progress("complete", current=len(details), total=len(details),
                            message=f"Done -- {stats['submissions_found']} submissions in {duration:.1f}s")
        fn_queries.finish_fn_poll_log(conn, log_id, "success", duration_seconds=duration, **stats)
        logger.info("fn poll complete in %.1fs -- %d submissions, %d snapshots",
                    duration, stats["submissions_found"], stats["snapshots_inserted"])

        if not is_first:
            from polling.telegram import send_poll_summary, check_milestones_batch, check_goals
            for coro, label in ((send_poll_summary("fn", stats, duration), "summary"),
                                (check_milestones_batch("fn", "fn_snapshots", "fn_submissions", account_id), "milestones"),
                                (check_goals(), "goals")):
                try:
                    await coro
                except Exception as te:
                    logger.warning("fn Telegram %s failed: %s", label, te, exc_info=True)

        return stats

    except Exception as e:
        duration = time.time() - start_time
        _update_fn_progress("error", message=describe_error(e))
        logger.error("fn poll failed: %s", describe_error(e), exc_info=True)
        if conn and log_id:
            fn_queries.finish_fn_poll_log(conn, log_id, "error", error_message=describe_error(e),
                                          duration_seconds=duration, **stats)
            conn.commit()
        from polling.telegram import send_poll_error
        try:
            await send_poll_error("fn", e)
        except Exception:
            logger.debug("Error alert send failed", exc_info=True)
        raise
    finally:
        _fn_first_poll_done.add(account_id)
        _fn_poll_lock.release()
        if conn:
            conn.close()
