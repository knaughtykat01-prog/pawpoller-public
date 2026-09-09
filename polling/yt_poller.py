"""YouTube (yt) poll cycle orchestration — MEDIAPLATS §6 (4.24.0).

Google OAuth user token (see clients/yt/client.py). Polls the channel's uploads
playlist and each video's statistics (views / likes / comments) into the
standard triple; the channel's subscriber count is the follower series (None
when the channel hides it). Mirrors polling/sc_poller.py; Google's refresh
token is stable, but the access token and its expiry are written back so the
next cycle (or the poster) does not spend a refresh needlessly.
"""
from __future__ import annotations

import atexit
import logging
import threading
import time
from datetime import datetime, timezone
from html import escape as _esc

import config
from clients.yt.client import YtClient
from database.db import get_connection
from database import yt_queries
from polling import notifications
from polling.followers import capture_followers
from polling.notifications import describe_error

logger = logging.getLogger(__name__)

yt_poll_progress = {"active": False, "phase": "idle", "current": 0, "total": 0, "message": ""}

_yt_poll_lock = threading.Lock()
_yt_first_poll_done: set[int] = set()
_yt_client: YtClient | None = None

TOKEN_FIELDS = ("yt_access_token", "yt_refresh_token", "yt_token_expires_at")


def _cleanup_yt_client():
    if _yt_client is not None:
        import asyncio
        try:
            asyncio.get_event_loop().run_until_complete(_yt_client.close())
        except Exception:
            logger.debug("yt client cleanup failed", exc_info=True)


atexit.register(_cleanup_yt_client)


def _update_yt_progress(phase: str, current: int = 0, total: int = 0, message: str = ""):
    yt_poll_progress.update(active=phase not in ("idle", "complete", "error"),
                            phase=phase, current=current, total=total, message=message)


def _send_yt_notifications(new_details: list[dict]) -> None:
    settings = config.get_settings()
    n = len(new_details)
    notifications.maybe_show_toast(
        settings, "sc_notifications_enabled",
        f"YouTube: {n} Video{'s' if n != 1 else ''} Updated",
        [f"{d['title'][:50]} gained activity" for d in new_details])


async def _send_yt_telegram(new_details: list[dict]) -> None:
    settings = config.get_settings()
    n = len(new_details)
    await notifications.maybe_send_telegram_summary(
        settings,
        f"<b>\U0001f3b5 YouTube: {n} Video{'s' if n != 1 else ''} Updated</b>",
        [_esc(d['title'][:50]) for d in new_details], log_label="YouTube")


def client_from_creds(creds: dict) -> YtClient:
    """A fresh client pointed at one account's credentials (the poster uses this too)."""
    try:
        expires_at = float(creds.get("yt_token_expires_at") or 0)
    except (TypeError, ValueError):
        expires_at = 0.0
    return YtClient(client_id=creds.get("yt_client_id", ""), client_secret=creds.get("yt_client_secret", ""),
                    access_token=creds.get("yt_access_token", ""), refresh_token=creds.get("yt_refresh_token", ""),
                    expires_at=expires_at)


def _get_or_create_client(creds: dict) -> YtClient:
    """Return the persistent YtClient, re-pointed at the account's credentials."""
    global _yt_client
    if _yt_client is None:
        _yt_client = client_from_creds(creds)
    else:
        _yt_client.client_id = creds.get("yt_client_id", "")
        _yt_client.client_secret = creds.get("yt_client_secret", "")
        if creds.get("yt_refresh_token") and creds["yt_refresh_token"] != _yt_client.refresh_token:
            # A different account (or a re-authorised one): take its pair, forget the old expiry.
            _yt_client.refresh_token = creds["yt_refresh_token"]
            _yt_client.access_token = creds.get("yt_access_token", "")
            _yt_client.expires_at = 0.0
            _yt_client._me = None
    _yt_client.tokens_changed = False
    return _yt_client


def token_updates(client: YtClient) -> dict:
    return {"yt_access_token": client.access_token, "yt_refresh_token": client.refresh_token,
            "yt_token_expires_at": str(int(client.expires_at or 0))}


def _persist_tokens(client: YtClient, account_id: int, is_default: bool) -> None:
    """Write the rotated pair back to this account's own keys. Every account, not only
    the default — a dropped rotation is a dead account at the next refresh."""
    if not client.tokens_changed or not client.refresh_token:
        return
    upd = {config.account_setting_key(account_id, k, is_default): v for k, v in token_updates(client).items()}
    try:
        config.save_settings(upd)
        client.tokens_changed = False
    except Exception:
        logger.debug("yt token persist failed", exc_info=True)


async def run_yt_poll_cycle(account_id: int | None = None, force_full: bool = False) -> dict:
    """Execute one complete YouTube poll cycle for a single account."""
    from database import accounts as accounts_db
    _ac = get_connection()
    try:
        if account_id is None:
            account_id = accounts_db.get_default_account_id(_ac, "yt", create=True)
        account_row = accounts_db.get_account(_ac, account_id)
    finally:
        _ac.close()
    is_default = bool(account_row["is_default"]) if account_row else True
    is_first = account_id not in _yt_first_poll_done

    if not _yt_poll_lock.acquire(blocking=False):
        logger.warning("yt poll already running -- skipping (account %s)", account_id)
        return {}
    _update_yt_progress("starting", message="Initialising YouTube poll cycle...")

    conn = None
    log_id = None
    start_time = time.time()
    stats = {"submissions_found": 0, "snapshots_inserted": 0}

    settings = config.get_settings()
    creds = config.resolve_account_credentials("yt", account_id, is_default, settings)
    client = _get_or_create_client(creds)

    try:
        conn = get_connection()
        log_id = yt_queries.start_yt_poll_log(conn, account_id)

        _update_yt_progress("searching", message="Authenticating with YouTube...")
        name = await client.validate_session()
        if not name:
            raise ValueError("YouTube is not authorised -- connect it in Settings (or the app's Testing-mode token expired)")
        _persist_tokens(client, account_id, is_default)

        _update_yt_progress("searching", message="Fetching your uploads...")
        ids = await client.get_my_video_ids()
        tracks = await client.get_videos(ids) if ids else []
        stats["submissions_found"] = len(tracks)

        if not tracks:
            _update_yt_progress("complete", message="No YouTube uploads found.")
            await capture_followers(client, account_id, conn)
            yt_queries.finish_yt_poll_log(conn, log_id, "success",
                                          duration_seconds=time.time() - start_time, **stats)
            conn.commit()
            return stats

        new_activity_details: list[dict] = []
        poll_ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        for idx, raw in enumerate(tracks, 1):
            _update_yt_progress("processing", current=idx, total=len(tracks),
                                message=f"Processing video {idx}/{len(tracks)}...")
            try:
                detail = raw
                sid = detail["submission_id"]
                if not sid:
                    continue
                views, faves, comments = detail["views"], detail["favorites_count"], detail["comments_count"]
                prev = yt_queries.get_yt_submission(conn, sid)
                if prev and (faves > prev.get("favorites_count", 0)
                             or comments > prev.get("comments_count", 0)):
                    new_activity_details.append({"title": detail.get("title", "")})
                yt_queries.upsert_yt_submission(conn, detail, account_id)
                yt_queries.insert_yt_snapshot(conn, account_id, sid, views, faves, comments, polled_at=poll_ts)
                stats["snapshots_inserted"] += 1
            except Exception as e:
                logger.warning("Error processing yt video %s: %s", str(raw.get("id", ""))[:50], e, exc_info=True)
        conn.commit()

        await capture_followers(client, account_id, conn)
        _persist_tokens(client, account_id, is_default)

        if is_first:
            logger.info("First yt poll for account %s -- suppressing %d activity notifications",
                        account_id, len(new_activity_details))
        else:
            try:
                _send_yt_notifications(new_activity_details)
            except Exception as ne:
                logger.warning("Failed to send sc notifications: %s", ne, exc_info=True)
            try:
                await _send_yt_telegram(new_activity_details)
            except Exception as te:
                logger.warning("Failed to send yt Telegram notification: %s", te, exc_info=True)

        duration = time.time() - start_time
        _update_yt_progress("complete", current=len(tracks), total=len(tracks),
                            message=f"Done -- {stats['submissions_found']} videos in {duration:.1f}s")
        yt_queries.finish_yt_poll_log(conn, log_id, "success", duration_seconds=duration, **stats)
        logger.info("yt poll complete in %.1fs -- %d videos, %d snapshots",
                    duration, stats["submissions_found"], stats["snapshots_inserted"])

        if not is_first:
            from polling.telegram import send_poll_summary, check_milestones_batch, check_goals
            for coro, label in ((send_poll_summary("yt", stats, duration), "summary"),
                                (check_milestones_batch("yt", "sc_snapshots", "sc_submissions", account_id), "milestones"),
                                (check_goals(), "goals")):
                try:
                    await coro
                except Exception as te:
                    logger.warning("yt Telegram %s failed: %s", label, te, exc_info=True)

        return stats

    except Exception as e:
        duration = time.time() - start_time
        _update_yt_progress("error", message=describe_error(e))
        logger.error("yt poll failed: %s", describe_error(e), exc_info=True)
        # Even a failed cycle may have rotated the pair on its way in.
        try:
            _persist_tokens(client, account_id, is_default)
        except Exception:
            pass
        if conn and log_id:
            yt_queries.finish_yt_poll_log(conn, log_id, "error", error_message=describe_error(e),
                                          duration_seconds=duration, **stats)
            conn.commit()
        from polling.telegram import send_poll_error
        try:
            await send_poll_error("yt", e)
        except Exception:
            logger.debug("Error alert send failed", exc_info=True)
        raise
    finally:
        _yt_first_poll_done.add(account_id)
        _yt_poll_lock.release()
        if conn:
            conn.close()
