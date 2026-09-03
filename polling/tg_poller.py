"""Telegram poll cycle — subscriber counts only.

Every other poller in this package fetches a list of submissions and a stats
row per submission. This one cannot, and the reason is worth stating plainly
because it looks like an omission:

* **Views do not exist.** The eye-count on a channel post is not in the Bot API
  at all — it is client-API only. There is nothing to fetch.
* **Reactions cannot be polled.** They arrive as pushed ``message_reaction_count``
  updates and there is no query-by-message endpoint, so they belong in the
  update loop (``polling/telegram_bot.py``), not here. See phase 3 of
  ``docs/specs/telegram_platform.md``.
* **Submissions need no fetching.** PawPoller sent every post itself and already
  records each one, so the list is exact rather than whatever a site chooses to
  return — the one place Telegram is *better* off than a polled platform.

That leaves the subscriber count, which `getChatMemberCount` gives in one call.
So this cycle is deliberately thin: it exists to give the shared follower
machinery an account and a connection to work with.
"""
from __future__ import annotations

import logging
import time as _time

import config
from database import tg_queries
from database.db import get_connection
from polling import followers as followers_mod

logger = logging.getLogger(__name__)

# Mirrors the other pollers' shape so the orchestrator can treat it the same.
progress: dict = {"running": False, "platform": "tg"}


async def run_tg_poll_cycle(account_id: int | None = None,
                            force_full: bool = False) -> dict:
    """Record one subscriber snapshot for a Telegram channel account.

    ``force_full`` is accepted for signature parity with every other cycle and
    has no meaning here — there is no back-catalogue to re-fetch.
    """
    result = {"platform": "tg", "account_id": account_id,
              "followers": None, "ok": False}
    conn = None
    log_id = None
    started = _time.time()
    try:
        settings = config.get_settings()
        from database import accounts as accounts_db
        conn = get_connection()
        if account_id is None:
            account_id = accounts_db.get_default_account_id(conn, "tg", create=True)
        row = accounts_db.get_account(conn, account_id)
        is_default = bool(row["is_default"]) if row else True

        creds = config.resolve_account_credentials("tg", account_id, is_default, settings)
        token = creds.get("tg_bot_token", "") or settings.get("telegram_bot_token", "")
        # ⚠ No flat fallback for the channel — inheriting another account's
        # channel would record ITS subscriber count against this account. Same
        # reasoning as the posting path.
        channel = creds.get("tg_channel", "")
        result["account_id"] = account_id
        if not token or not channel:
            # Deliberately BEFORE the log is opened. An uncredentialed account
            # would otherwise write an 'error' row every cycle and paint the
            # status dot red, when the honest reading is "not set up yet" —
            # which the health endpoint already reports from `configured`.
            result["error"] = "not configured"
            return result

        log_id = tg_queries.start_tg_poll_log(conn, account_id)

        from clients.tg.client import TgClient
        try:
            client = TgClient(bot_token=token, channel=channel)
        except ValueError as e:      # invite link, etc.
            result["error"] = str(e)
            return result

        # capture_followers does the network call BEFORE any write, so no
        # SQLite write transaction is held across an await (2.26.3).
        wrote = await followers_mod.capture_followers(client, account_id, conn)
        result["ok"] = bool(wrote)
        result["account_id"] = account_id
        if not wrote:
            # capture_followers only returns a bool, so without this the poll
            # log recorded status='error' with error_message NULL — a red dot
            # and nothing to act on. TgClient.last_error holds Telegram's own
            # words ("chat not found", "bot is not a member of the channel
            # chat"), which is exactly what tells the user what to fix.
            result["error"] = (getattr(client, "last_error", "")
                               or "Telegram returned no subscriber count")
        # capture_followers returns only a bool, and calling the API a second
        # time to learn the number would double the request. Read back the
        # cached value it just wrote instead.
        if wrote:
            cached = conn.execute(
                "SELECT follower_count FROM accounts WHERE account_id = ?",
                (account_id,)).fetchone()
            if cached is not None:
                result["followers"] = cached[0]
            logger.info("TG: account %s has %s subscribers", account_id,
                        result["followers"])
        else:
            logger.warning("TG: no subscriber count for account %s (%s)",
                           account_id, result["error"])
        return result
    except Exception as e:
        logger.warning("TG poll cycle failed for account %s: %s", account_id, e)
        result["error"] = str(e)
        return result
    finally:
        # Closed here rather than on each return so every exit path lands in
        # the log — including the two early returns above, which is exactly
        # where a "why is there no status dot?" report comes from.
        if conn is not None and log_id is not None:
            try:
                tg_queries.finish_tg_poll_log(
                    conn, log_id,
                    "success" if result.get("ok") else "error",
                    snapshots_inserted=1 if result.get("ok") else 0,
                    error_message=result.get("error"),
                    duration_seconds=round(_time.time() - started, 2))
            except Exception as e:      # a log write must never fail a poll
                logger.warning("TG: could not finish poll log: %s", e)
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
