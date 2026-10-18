"""Twitch (ttv) poll cycle — spec 045.

Per account, all reads before any DB write (poller gotcha: no write lock across an await):
  1. the channel by its permanent id (a rename updates the stored login);
  2. live now? (title, game, viewers) — also fed to the go-live engine as a check;
  3. past broadcasts + clips, each with Twitch's view count;
  4. with Connect only: followers, subscribers + sub points, Bits from the top cheerers.
     A part Twitch refuses (a scope not granted) is left NULL and the rest still lands.
"""
from __future__ import annotations

import logging
import os
import threading
import time
from datetime import datetime, timezone

import config
from clients.ttv.client import TtvAuthError, TtvClient
from database.db import get_connection
from database import ttv_queries
from polling.followers import capture_followers
from polling.notifications import describe_error
from polling.site_turns import site_turn

logger = logging.getLogger(__name__)

ttv_poll_progress = {"active": False, "phase": "idle", "current": 0, "total": 0, "message": ""}

_ttv_poll_lock = threading.Lock()

TOKEN_FIELDS = ("ttv_access_token", "ttv_refresh_token", "ttv_token_expires_at")


def _progress(phase: str, message: str = "", current: int = 0, total: int = 0) -> None:
    ttv_poll_progress.update(active=phase not in ("idle", "complete", "error"),
                             phase=phase, current=current, total=total, message=message)


def app_creds(creds: dict) -> tuple[str, str]:
    """The Twitch app: this account's (or the default account's) pair, else the environment's (a hosted seat)."""
    cid, secret = creds.get("ttv_client_id") or "", creds.get("ttv_client_secret") or ""
    if cid and secret:
        return cid, secret
    return (os.environ.get("PAWPOLLER_TWITCH_CLIENT_ID") or "").strip(), \
        (os.environ.get("PAWPOLLER_TWITCH_CLIENT_SECRET") or "").strip()


def make_client(creds: dict) -> TtvClient:
    cid, secret = app_creds(creds)
    return TtvClient(cid, secret, login=creds.get("ttv_channel", ""), user_id=creds.get("ttv_user_id", ""),
                     access_token=creds.get("ttv_access_token", ""), refresh_token=creds.get("ttv_refresh_token", ""),
                     token_expires_at=float(creds.get("ttv_token_expires_at") or 0))


def account_creds(account_id: int) -> tuple[dict, bool]:
    from database import accounts as accounts_db
    conn = get_connection()
    try:
        row = accounts_db.get_account(conn, account_id)
    finally:
        conn.close()
    is_default = bool(row["is_default"]) if row else True
    return config.resolve_account_credentials("ttv", account_id, is_default, config.get_settings()), is_default


def save_tokens(client: TtvClient, account_id: int, is_default: bool) -> None:
    """A renewed user token is written back so the next poll doesn't renew again."""
    if not client.tokens_changed:
        return
    config.save_settings({config.account_setting_key(account_id, f, is_default): v for f, v in (
        ("ttv_access_token", client.access_token), ("ttv_refresh_token", client.refresh_token),
        ("ttv_token_expires_at", client.token_expires_at))})
    client.tokens_changed = False


async def _connected_numbers(client: TtvClient) -> dict:
    out: dict = {"followers": None, "subscribers": None, "sub_points": None, "bits_top": None}
    if not client.connected:
        return out
    for key, call in (("followers", client.get_follower_count), ("subs", client.get_subscriptions),
                      ("bits_top", client.get_bits_top)):
        try:
            v = await call()
        except TtvAuthError as e:
            logger.info("ttv: %s not read (%s)", key, e)
            continue
        if key == "subs":
            out["subscribers"], out["sub_points"] = v
        else:
            out[key] = v
    return out


@site_turn("ttv")
async def run_ttv_poll_cycle(account_id: int | None = None, force_full: bool = False,
                             client: TtvClient | None = None) -> dict:
    """One Twitch poll for one account. ``client`` is for tests. Every poll is already full."""
    from database import accounts as accounts_db
    if account_id is None:
        _ac = get_connection()
        try:
            account_id = accounts_db.get_default_account_id(_ac, "ttv", create=True)
        finally:
            _ac.close()
    creds, is_default = account_creds(account_id)

    if not _ttv_poll_lock.acquire(blocking=False):
        logger.warning("ttv poll already running -- skipping (account %s)", account_id)
        return {}
    _progress("starting", "Reading your Twitch channel...")

    conn = None
    log_id = None
    start = time.time()
    stats = {"submissions_found": 0, "snapshots_inserted": 0}
    own = client is None
    client = client or make_client(creds)
    try:
        conn = get_connection()
        log_id = ttv_queries.start_ttv_poll_log(conn, account_id)
        conn.close()
        conn = None

        if not client.user_id:
            raise ValueError("No Twitch channel is set -- add it in Settings → Platforms → Twitch")
        user = await client.get_user()
        if not user:
            raise ValueError("Twitch has no channel with this id any more -- add the channel again in Settings")
        if user["login"] and user["login"] != client.login:
            client.login = user["login"]          # renamed on Twitch: same id, new name (FR-001)
            config.save_settings({config.account_setting_key(account_id, "ttv_channel", is_default): user["login"]})
        live = (await client.get_streams([client.user_id])).get(client.user_id)
        _progress("searching", "Reading your past broadcasts and clips...")
        items = await client.get_videos() + await client.get_clips()
        extra = await _connected_numbers(client)
        save_tokens(client, account_id, is_default)

        poll_ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        conn = get_connection()
        ttv_queries.insert_ttv_channel_snapshot(conn, account_id, {
            "channel": user["login"], "live": bool(live), "viewers": (live or {}).get("viewers", 0),
            "title": (live or {}).get("title", ""), "game": (live or {}).get("game", ""),
            "connected": client.connected, **extra}, poll_ts)
        for item in items:
            ttv_queries.upsert_ttv_item(conn, item, account_id, poll_ts)
        stats.update(submissions_found=len(items), snapshots_inserted=len(items) + 1)
        conn.commit()

        if extra.get("followers") is not None:
            await capture_followers(_Known(extra["followers"]), account_id, conn)

        try:                                     # the stats poll is also a live check for go-live posts
            from polling import golive
            await golive.on_check_state("ttv", account_id, live)
        except Exception:
            logger.warning("ttv: go-live check from the poll failed", exc_info=True)

        duration = time.time() - start
        _progress("complete", f"Done -- {user['login']}: {'live now' if live else 'offline'}, "
                              f"{len(items)} broadcasts and clips")
        ttv_queries.finish_ttv_poll_log(conn, log_id, "success", duration_seconds=duration, **stats)
        logger.info("ttv poll complete in %.1fs -- %d items", duration, len(items))
        return stats
    except Exception as e:
        duration = time.time() - start
        _progress("error", describe_error(e))
        logger.error("ttv poll failed: %s", describe_error(e), exc_info=True)
        if log_id:
            c = conn or get_connection()
            try:
                ttv_queries.finish_ttv_poll_log(c, log_id, "error", error_message=describe_error(e),
                                                duration_seconds=duration, **stats)
            finally:
                if c is not conn:
                    c.close()
        from polling.telegram import send_poll_error
        try:
            await send_poll_error("ttv", e)
        except Exception:
            logger.debug("Error alert send failed", exc_info=True)
        raise
    finally:
        _ttv_poll_lock.release()
        if conn:
            conn.close()
        if own:
            await client.close()


class _Known:
    """A follower count already read this poll, in the shape capture_followers asks for."""

    def __init__(self, n: int):
        self._n = n

    async def get_follower_count(self) -> int:
        return self._n
