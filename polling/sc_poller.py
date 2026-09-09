"""SoundCloud (sc) poll cycle orchestration — MEDIAPLATS §3 (4.22.0).

OAuth 2.1 user token (see clients/sc/client.py). Polls the connected user's own
tracks (``GET /me/tracks``) and snapshots plays / likes / comments; SoundCloud
exposes ``followers_count`` on ``/me``, so the follower series is captured too.
Mirrors polling/fn_poller.py.

**Tokens rotate.** SoundCloud's refresh token is single-use, so the pair is
written back to THIS account's own keys after every cycle that refreshed
(``_persist_tokens``) — the default account's bare keys or the namespaced
``acct_<id>_sc_*`` ones, per ``config.account_setting_key``.
"""
from __future__ import annotations

import atexit
import logging
import threading
import time
from datetime import datetime, timezone
from html import escape as _esc

import config
from clients.sc.client import ScClient
from database.db import get_connection
from database import sc_queries
from polling import notifications
from polling.followers import capture_followers
from polling.notifications import describe_error

logger = logging.getLogger(__name__)

sc_poll_progress = {"active": False, "phase": "idle", "current": 0, "total": 0, "message": ""}

_sc_poll_lock = threading.Lock()
_sc_first_poll_done: set[int] = set()
_sc_client: ScClient | None = None

TOKEN_FIELDS = ("sc_access_token", "sc_refresh_token", "sc_token_expires_at")


def _cleanup_sc_client():
    if _sc_client is not None:
        import asyncio
        try:
            asyncio.get_event_loop().run_until_complete(_sc_client.close())
        except Exception:
            logger.debug("sc client cleanup failed", exc_info=True)


atexit.register(_cleanup_sc_client)


def _update_sc_progress(phase: str, current: int = 0, total: int = 0, message: str = ""):
    sc_poll_progress.update(active=phase not in ("idle", "complete", "error"),
                            phase=phase, current=current, total=total, message=message)


def _send_sc_notifications(new_details: list[dict]) -> None:
    settings = config.get_settings()
    n = len(new_details)
    notifications.maybe_show_toast(
        settings, "sc_notifications_enabled",
        f"SoundCloud: {n} Track{'s' if n != 1 else ''} Updated",
        [f"{d['title'][:50]} gained activity" for d in new_details])


async def _send_sc_telegram(new_details: list[dict]) -> None:
    settings = config.get_settings()
    n = len(new_details)
    await notifications.maybe_send_telegram_summary(
        settings,
        f"<b>\U0001f3b5 SoundCloud: {n} Track{'s' if n != 1 else ''} Updated</b>",
        [_esc(d['title'][:50]) for d in new_details], log_label="SoundCloud")


def client_from_creds(creds: dict) -> ScClient:
    """A fresh client pointed at one account's credentials (the poster uses this too)."""
    try:
        expires_at = float(creds.get("sc_token_expires_at") or 0)
    except (TypeError, ValueError):
        expires_at = 0.0
    return ScClient(client_id=creds.get("sc_client_id", ""), client_secret=creds.get("sc_client_secret", ""),
                    access_token=creds.get("sc_access_token", ""), refresh_token=creds.get("sc_refresh_token", ""),
                    expires_at=expires_at)


def _get_or_create_client(creds: dict) -> ScClient:
    """Return the persistent ScClient, re-pointed at the account's credentials."""
    global _sc_client
    if _sc_client is None:
        _sc_client = client_from_creds(creds)
    else:
        _sc_client.client_id = creds.get("sc_client_id", "")
        _sc_client.client_secret = creds.get("sc_client_secret", "")
        if creds.get("sc_refresh_token") and creds["sc_refresh_token"] != _sc_client.refresh_token:
            # A different account (or a re-authorised one): take its pair, forget the old expiry.
            _sc_client.refresh_token = creds["sc_refresh_token"]
            _sc_client.access_token = creds.get("sc_access_token", "")
            _sc_client.expires_at = 0.0
            _sc_client._me = None
    _sc_client.tokens_changed = False
    return _sc_client


def token_updates(client: ScClient) -> dict:
    return {"sc_access_token": client.access_token, "sc_refresh_token": client.refresh_token,
            "sc_token_expires_at": str(int(client.expires_at or 0))}


def _persist_tokens(client: ScClient, account_id: int, is_default: bool) -> None:
    """Write the rotated pair back to this account's own keys. Every account, not only
    the default — a dropped rotation is a dead account at the next refresh."""
    if not client.tokens_changed or not client.refresh_token:
        return
    upd = {config.account_setting_key(account_id, k, is_default): v for k, v in token_updates(client).items()}
    try:
        config.save_settings(upd)
        client.tokens_changed = False
    except Exception:
        logger.debug("sc token persist failed", exc_info=True)


async def run_sc_poll_cycle(account_id: int | None = None, force_full: bool = False) -> dict:
    """Execute one complete SoundCloud poll cycle for a single account."""
    from database import accounts as accounts_db
    _ac = get_connection()
    try:
        if account_id is None:
            account_id = accounts_db.get_default_account_id(_ac, "sc", create=True)
        account_row = accounts_db.get_account(_ac, account_id)
    finally:
        _ac.close()
    is_default = bool(account_row["is_default"]) if account_row else True
    is_first = account_id not in _sc_first_poll_done

    if not _sc_poll_lock.acquire(blocking=False):
        logger.warning("sc poll already running -- skipping (account %s)", account_id)
        return {}
    _update_sc_progress("starting", message="Initialising SoundCloud poll cycle...")

    conn = None
    log_id = None
    start_time = time.time()
    stats = {"submissions_found": 0, "snapshots_inserted": 0}

    settings = config.get_settings()
    creds = config.resolve_account_credentials("sc", account_id, is_default, settings)
    client = _get_or_create_client(creds)

    try:
        conn = get_connection()
        log_id = sc_queries.start_sc_poll_log(conn, account_id)

        _update_sc_progress("searching", message="Authenticating with SoundCloud...")
        name = await client.validate_session()
        if not name:
            raise ValueError("SoundCloud is not authorised -- connect it in Settings")
        _persist_tokens(client, account_id, is_default)

        _update_sc_progress("searching", message="Fetching your tracks...")
        tracks = await client.get_my_tracks()
        stats["submissions_found"] = len(tracks)

        if not tracks:
            _update_sc_progress("complete", message="No SoundCloud tracks found.")
            await capture_followers(client, account_id, conn)
            sc_queries.finish_sc_poll_log(conn, log_id, "success",
                                          duration_seconds=time.time() - start_time, **stats)
            conn.commit()
            return stats

        new_activity_details: list[dict] = []
        poll_ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        for idx, raw in enumerate(tracks, 1):
            _update_sc_progress("processing", current=idx, total=len(tracks),
                                message=f"Processing track {idx}/{len(tracks)}...")
            try:
                detail = ScClient.parse_track(raw)
                sid = detail["submission_id"]
                if not sid:
                    continue
                views, faves, comments = detail["views"], detail["favorites_count"], detail["comments_count"]
                prev = sc_queries.get_sc_submission(conn, sid)
                if prev and (faves > prev.get("favorites_count", 0)
                             or comments > prev.get("comments_count", 0)):
                    new_activity_details.append({"title": detail.get("title", "")})
                sc_queries.upsert_sc_submission(conn, detail, account_id)
                sc_queries.insert_sc_snapshot(conn, account_id, sid, views, faves, comments, polled_at=poll_ts)
                stats["snapshots_inserted"] += 1
            except Exception as e:
                logger.warning("Error processing sc track %s: %s", str(raw.get("id", ""))[:50], e, exc_info=True)
        conn.commit()

        await capture_followers(client, account_id, conn)
        _persist_tokens(client, account_id, is_default)

        if is_first:
            logger.info("First sc poll for account %s -- suppressing %d activity notifications",
                        account_id, len(new_activity_details))
        else:
            try:
                _send_sc_notifications(new_activity_details)
            except Exception as ne:
                logger.warning("Failed to send sc notifications: %s", ne, exc_info=True)
            try:
                await _send_sc_telegram(new_activity_details)
            except Exception as te:
                logger.warning("Failed to send sc Telegram notification: %s", te, exc_info=True)

        duration = time.time() - start_time
        _update_sc_progress("complete", current=len(tracks), total=len(tracks),
                            message=f"Done -- {stats['submissions_found']} tracks in {duration:.1f}s")
        sc_queries.finish_sc_poll_log(conn, log_id, "success", duration_seconds=duration, **stats)
        logger.info("sc poll complete in %.1fs -- %d tracks, %d snapshots",
                    duration, stats["submissions_found"], stats["snapshots_inserted"])

        if not is_first:
            from polling.telegram import send_poll_summary, check_milestones_batch, check_goals
            for coro, label in ((send_poll_summary("sc", stats, duration), "summary"),
                                (check_milestones_batch("sc", "sc_snapshots", "sc_submissions", account_id), "milestones"),
                                (check_goals(), "goals")):
                try:
                    await coro
                except Exception as te:
                    logger.warning("sc Telegram %s failed: %s", label, te, exc_info=True)

        return stats

    except Exception as e:
        duration = time.time() - start_time
        _update_sc_progress("error", message=describe_error(e))
        logger.error("sc poll failed: %s", describe_error(e), exc_info=True)
        # Even a failed cycle may have rotated the pair on its way in.
        try:
            _persist_tokens(client, account_id, is_default)
        except Exception:
            pass
        if conn and log_id:
            sc_queries.finish_sc_poll_log(conn, log_id, "error", error_message=describe_error(e),
                                          duration_seconds=duration, **stats)
            conn.commit()
        from polling.telegram import send_poll_error
        try:
            await send_poll_error("sc", e)
        except Exception:
            logger.debug("Error alert send failed", exc_info=True)
        raise
    finally:
        _sc_first_poll_done.add(account_id)
        _sc_poll_lock.release()
        if conn:
            conn.close()
