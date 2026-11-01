"""Toyhouse (th) poll cycle — spec 062.

Cookie session, HTML read (clients/th/client.py). Two kinds of list page carry every number, so a poll is a
handful of page loads with no per-item fetches: the owner's art list (each image's favourites and comment
count) and the owner's character list (each character's favourites, images and comments). Toyhouse shows no
view counts, so ``views`` stays 0. New comments on images and characters go to the Inbox through the shared
capture. A bot check starts the shared stand-down (``fa_standdown`` with ``site="th"``) and is never treated as
a sign-in problem (FR-SEC-008).
"""
from __future__ import annotations

import asyncio
import atexit
import logging
import threading
import time
from datetime import datetime, timezone

import config
from clients.th.client import MAX_PAGES, PAGE_PAUSE_S, RECENT_PAGES, THChallenged, THClient, THUnavailable
from database import th_queries
from database.db import get_connection
from polling import fa_standdown, loop_bound, notifications
from polling.notifications import describe_error
from polling.site_turns import site_turn

logger = logging.getLogger(__name__)

th_poll_progress = {"active": False, "phase": "idle", "current": 0, "total": 0, "message": ""}

# FR-009b: an ordinary poll reads this many characters' own pages, longest-unread first (each about once a day
# at six polls a day); the weekly full read reads them all. ponytail: a burst of favourites on one character shows
# on its next turn, not at once.
CHARACTERS_PER_POLL = 20

_th_poll_lock = threading.Lock()
_th_first_poll_done: set[int] = set()
_th_client: THClient | None = None
_skip_recorded_until = 0.0


def _cleanup_th_client():
    if _th_client is not None:
        import asyncio
        try:
            asyncio.get_event_loop().run_until_complete(_th_client.close())
        except Exception:
            logger.debug("th client cleanup failed", exc_info=True)


atexit.register(_cleanup_th_client)


def _update_th_progress(phase: str, current: int = 0, total: int = 0, message: str = ""):
    th_poll_progress.update(active=phase not in ("idle", "complete", "error"),
                            phase=phase, current=current, total=total, message=message)


def client_from_creds(creds: dict) -> THClient:
    """A fresh client pointed at one account's cookie + username (the poster and routes use this too)."""
    return THClient(username=creds.get("th_username", ""), cookie=creds.get("th_cookie", ""))


def _get_or_create_client(creds: dict) -> THClient:
    global _th_client
    if not loop_bound.reusable(_th_client) or _th_client.cookie != (creds.get("th_cookie") or "").strip() \
            or _th_client.username != (creds.get("th_username") or "").strip():
        _th_client = client_from_creds(creds)
    return loop_bound.pin(_th_client)


@site_turn("th")
async def run_th_poll_cycle(account_id: int | None = None, force_full: bool = False) -> dict:
    """Execute one complete Toyhouse poll cycle for a single account."""
    global _skip_recorded_until
    from database import accounts as accounts_db
    _ac = get_connection()
    try:
        if account_id is None:
            account_id = accounts_db.get_default_account_id(_ac, "th", create=True)
        account_row = accounts_db.get_account(_ac, account_id)
    finally:
        _ac.close()
    is_default = bool(account_row["is_default"]) if account_row else True
    is_first = account_id not in _th_first_poll_done

    if fa_standdown.remaining("th") > 0:
        reason = f"Toyhouse's bot check is on, next try around {fa_standdown.next_try_text('th')}"
        if _skip_recorded_until != fa_standdown.until("th"):       # one log row per stand-down, not per tick
            _skip_recorded_until = fa_standdown.until("th")
            c = get_connection()
            try:
                lid = th_queries.start_th_poll_log(c, account_id)
                th_queries.finish_th_poll_log(c, lid, "skipped", error_message=reason)
            finally:
                c.close()
        logger.info("th poll skipped: %s", reason)
        return {"skipped": reason}

    if not _th_poll_lock.acquire(blocking=False):
        logger.warning("th poll already running -- skipping (account %s)", account_id)
        return {}
    _update_th_progress("starting", message="Starting the Toyhouse poll...")

    conn = None
    log_id = None
    start_time = time.time()
    stats = {"submissions_found": 0, "snapshots_inserted": 0}
    creds = config.resolve_account_credentials("th", account_id, is_default, config.get_settings())
    client = _get_or_create_client(creds)

    try:
        conn = get_connection()
        # Every page once a week (and on an account's first poll); otherwise the newest pages, where new
        # favourites and comments land. A big gallery is 100+ pages, minutes at a polite pace.
        full = force_full or th_queries.full_sweep_due(conn, account_id)
        pages = MAX_PAGES if full else RECENT_PAGES
        log_id = th_queries.start_th_poll_log(conn, account_id)

        _update_th_progress("searching", message="Checking the Toyhouse sign-in...")
        session = await client.validate_session()
        if not session.get("ok"):
            raise ValueError(f"Toyhouse sign-in check failed -- {session.get('detail') or 'sign in again'}")

        _update_th_progress("searching", message="Reading all your art and characters (weekly)..." if full
                            else "Reading your newest art and characters...")
        images = await client.get_all_images(pages)
        characters = await client.get_characters(pages)
        stats["submissions_found"] = len(images)

        new_activity: list[dict] = []
        poll_ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        for img in images:
            sid = img["submission_id"]
            prev = th_queries.get_th_submission(conn, sid)
            faves, comments = img["favorites_count"], img["comments_count"]
            th_queries.upsert_th_submission(conn, img, account_id)
            if faves is None or comments is None:
                continue        # this layout shows no image counts (research R9): no snapshot, no "new" alert
            if prev and prev.get("favorites_count") is not None and (
                    faves > (prev.get("favorites_count") or 0) or comments > (prev.get("comments_count") or 0)):
                new_activity.append(notifications.activity_item(
                    sid, img.get("title", ""), faves=faves - (prev.get("favorites_count") or 0),
                    comments=comments - (prev.get("comments_count") or 0)))
            th_queries.insert_th_snapshot(conn, account_id, sid, 0, faves, comments, polled_at=poll_ts)
            stats["snapshots_inserted"] += 1
        before = {c["character_id"]: c for c in th_queries.get_th_characters(conn, account_id)}
        for ch in characters:
            th_queries.upsert_th_character(conn, ch, account_id)
        ids = [c["character_id"] for c in characters]
        to_read = ids if full else th_queries.characters_to_read(conn, account_id, ids, CHARACTERS_PER_POLL)
        conn.commit()   # before the page reads: never hold the write lock across an await

        # Character counts live on each character's own page (research R9, FR-009). One page each,
        # PAGE_PAUSE_S apart; a busy site stops the reads for this poll, a bot check stands the poll down.
        _update_th_progress("searching", message=f"Reading {len(to_read)} characters' pages...")
        names = {c["character_id"]: c.get("name", "") for c in characters}
        read: dict[str, dict] = {}
        for n, cid in enumerate(to_read):
            if n:
                await asyncio.sleep(PAGE_PAUSE_S)
            try:
                got = await client.get_character_stats(cid)
            except THUnavailable as e:
                logger.warning("th: stopped reading character pages after %d of %d: %s", n, len(to_read), e)
                break
            if got is None:
                continue        # gone, or a page with no sidebar (a content warning): keep what we had
            old = before.get(cid) or {}
            if old.get("read_at") and (got["favorites_count"] > (old.get("favorites_count") or 0)
                                       or got["comments_count"] > (old.get("comments_count") or 0)):
                new_activity.append(notifications.activity_item(
                    f"c{cid}", names.get(cid, ""), faves=got["favorites_count"] - (old.get("favorites_count") or 0),
                    comments=got["comments_count"] - (old.get("comments_count") or 0)))
            th_queries.record_character_stats(conn, cid, got)
            conn.commit()
            read[cid] = got
        th_queries.record_character_totals(conn, account_id, polled_at=poll_ts)
        conn.commit()

        # Comments → Inbox. Image threads keyed by the image id, character threads by "c{id}".
        from polling.inbox_capture import capture as _inbox_capture
        # Only threads whose count this poll actually read: an unknown count can't say "something new".
        cands = [{"submission_id": i["submission_id"], "fresh_count": i["comments_count"], "title": i["title"],
                  "character_id": i["character_id"]} for i in images
                 if i.get("character_id") and i.get("comments_count") is not None]
        cands += [{"submission_id": f"c{cid}", "fresh_count": st["comments_count"],
                   "title": names.get(cid, ""), "character_id": cid} for cid, st in read.items()]

        async def _fetch(c):
            if c["submission_id"].startswith("c"):
                return await client.get_character_comments(c["character_id"])
            return await client.get_image_comments(c["character_id"], c["submission_id"])
        try:
            await _inbox_capture(conn, "th", cands, _fetch, account_id=account_id, own_author=client.username)
        except Exception as ce:  # noqa: BLE001 — capture must never fail a poll
            logger.warning("th inbox capture failed: %s", ce)

        await fa_standdown.reset("th")
        if is_first:
            logger.info("First th poll for account %s -- suppressing %d activity notifications",
                        account_id, len(new_activity))
        else:
            try:
                await notifications.send_activity_alert("th", new_activity, settings_key="th_notifications_enabled")
            except Exception as te:
                logger.warning("Failed to send th activity alert: %s", te, exc_info=True)

        duration = time.time() - start_time
        _update_th_progress("complete", current=len(images), total=len(images),
                            message=f"Done -- {len(images)} images, {len(characters)} characters in {duration:.1f}s")
        th_queries.finish_th_poll_log(conn, log_id, "success", duration_seconds=duration, full_sweep=full, **stats)
        logger.info("th poll complete in %.1fs -- %d images, %d characters, %d character pages read (%s)",
                    duration, len(images), len(characters), len(read),
                    "every page" if full else f"newest {pages} pages")

        if not is_first:
            from polling.telegram import check_goals, check_milestones_batch, send_poll_summary
            for coro, label in ((send_poll_summary("th", stats, duration), "summary"),
                                (check_milestones_batch("th", "th_snapshots", "th_submissions", account_id), "milestones"),
                                (check_goals(), "goals")):
                try:
                    await coro
                except Exception as te:
                    logger.warning("th Telegram %s failed: %s", label, te, exc_info=True)
        return stats

    except Exception as e:
        if isinstance(e, THChallenged):
            fa_standdown.note_challenge("th")
            e = THChallenged(f"Toyhouse's bot check turned PawPoller away. Next try around "
                             f"{fa_standdown.next_try_text('th')}.")
        duration = time.time() - start_time
        _update_th_progress("error", message=describe_error(e))
        logger.error("th poll failed: %s", describe_error(e))
        if conn and log_id:
            th_queries.finish_th_poll_log(conn, log_id, "error", error_message=describe_error(e),
                                          duration_seconds=duration, **stats)
            conn.commit()
        from polling.telegram import send_poll_error
        try:
            await send_poll_error("th", e)
        except Exception:
            logger.debug("Error alert send failed", exc_info=True)
        raise e from None
    finally:
        _th_first_poll_done.add(account_id)
        _th_poll_lock.release()
        if conn:
            conn.close()
