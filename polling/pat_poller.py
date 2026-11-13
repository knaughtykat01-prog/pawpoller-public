"""Patreon (pat) poll cycle — spec 084.

Through the creator's signed-in session (``clients/pat/client.py``). One poll:

1. the page check: the session's campaign must be the saved one (a different page is named, never polled);
2. the tier list and the page's 18+ flag are refreshed (``pat_tiers`` / ``pat_is_nsfw``, from this read only);
3. each published post's likes and comments → ``pat_submissions`` / ``pat_snapshots`` (Patreon shows no views);
4. supporter totals: the campaign's own figures plus every member, counted in memory and stored as totals only
   (``database/supporter_queries.py``). A member read that stops part-way writes no supporter numbers at all;
5. new comments → the Inbox through the shared capture.

A bot check or a 429 starts the shared stand-down (``fa_standdown`` with ``site="pat"``) and is said as such, never
as "signed out". Nothing is held open across a network call: each write is committed before the next read.
"""
from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timezone

import config
from clients.pat import account as pat_account
from clients.pat.client import PatAuthError, PatChallenged, PatClient, PatPartial, PAUSED
from database import pat_queries, supporter_queries
from database.db import get_connection
from polling import fa_standdown, notifications
from polling.notifications import describe_error
from polling.site_turns import site_turn

logger = logging.getLogger(__name__)

pat_poll_progress = {"active": False, "phase": "idle", "current": 0, "total": 0, "message": ""}
_lock = threading.Lock()
_first_done: set[int] = set()
# Stand-in transport for tests; None = the real network.
_transport = None


def _progress(phase: str, current: int = 0, total: int = 0, message: str = "") -> None:
    pat_poll_progress.update(active=phase not in ("idle", "complete", "error"), phase=phase, current=current,
                             total=total, message=message)


@site_turn("pat")
async def run_pat_poll_cycle(account_id: int | None = None, force_full: bool = False) -> dict:
    """One complete Patreon poll for one account. ``force_full`` is accepted for the shared trigger route."""
    from database import accounts as accounts_db
    c0 = get_connection()
    try:
        aid = account_id if account_id is not None else accounts_db.get_default_account_id(c0, "pat", create=True)
        row = accounts_db.get_account(c0, aid) if aid is not None else None
    finally:
        c0.close()
    if aid is None:            # create=True always makes one; this keeps a None out of every write below
        return {"skipped": "There's no Patreon account to poll."}
    is_default = bool(row["is_default"]) if row else True

    if fa_standdown.remaining("pat") > 0:
        reason = PAUSED.format(when=fa_standdown.next_try_text('pat'))
        logger.info("pat poll skipped: %s", reason)
        return {"skipped": reason}
    vals = pat_account.values(aid)
    if not vals["campaign_id"]:
        return {"skipped": "Sign in to Patreon again: PawPoller doesn't know which page this is."}
    if not _lock.acquire(blocking=False):
        logger.warning("pat poll already running -- skipping (account %s)", aid)
        return {}

    stats = {"submissions_found": 0, "snapshots_inserted": 0}
    partial: str | None = None          # why the supporter numbers weren't written this poll
    start = time.time()
    conn = log_id = None
    is_first = aid not in _first_done
    try:
        creds = config.resolve_account_credentials("pat", aid, is_default, config.get_settings())
        conn = get_connection()
        log_id = pat_queries.start_pat_poll_log(conn, aid)
        async with PatClient(creds.get("pat_cookie") or "", transport=_transport) as client:
            _progress("searching", message="Checking the Patreon sign-in...")
            camp = await client.current_campaign()
            if camp["campaign_id"] != vals["campaign_id"]:
                raise PatAuthError(f"This Patreon sign-in is for a different page ({camp['vanity'] or camp['campaign_id']}) "
                                   "than the saved one. Sign in to the right page again.")
            cid = camp["campaign_id"]
            pat_account.record_campaign(aid, camp, await client.audience_rules(cid))

            _progress("searching", message="Reading your Patreon posts...")
            posts = await client.posts(cid)
            stats["submissions_found"] = len(posts)
            poll_ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
            activity = []
            for p in posts:
                prev = pat_queries.get_pat_submission(conn, p["submission_id"])
                pat_queries.upsert_pat_submission(conn, {**p, "username": camp["vanity"]}, aid)
                pat_queries.insert_pat_snapshot(conn, aid, p["submission_id"], p["favorites_count"],
                                                p["comments_count"], polled_at=poll_ts)
                stats["snapshots_inserted"] += 1
                if prev and (p["favorites_count"] > (prev.get("favorites_count") or 0)
                             or p["comments_count"] > (prev.get("comments_count") or 0)):
                    activity.append(notifications.activity_item(
                        p["submission_id"], p["title"], faves=p["favorites_count"] - (prev.get("favorites_count") or 0),
                        comments=p["comments_count"] - (prev.get("comments_count") or 0)))
            conn.commit()      # before the member read: never hold the write lock across an await

            _progress("searching", message="Counting your supporters...")
            figures = await client.campaign_figures(cid)
            member_ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")   # "new since" counts from here
            try:
                members = await client.members(cid)
            except PatPartial as e:
                members = None
                partial = str(e)
                logger.warning("pat: supporter numbers not written this poll: %s", e)
            if members is not None:
                snap = supporter_queries.figures(members, figures,
                                                 supporter_queries.last_polled_at(conn, aid))
                del members      # the per-member records go no further than this count
                supporter_queries.record_snapshot(conn, platform="pat", account_id=aid, polled_at=member_ts, **snap)
                conn.commit()

            slowed: list[PatChallenged] = []     # capture() swallows fetch errors, so a slow-down is carried out here
            try:
                from polling.inbox_capture import capture
                own = camp["vanity"] or "creator"
                cands = [{"submission_id": p["submission_id"], "fresh_count": p["comments_count"], "title": p["title"]}
                         for p in posts if p["comments_count"]]      # capture's own cap picks which (25)

                async def _fetch(cand):
                    if slowed:
                        return []
                    try:
                        got = await client.comments(cand["submission_id"])
                    except PatChallenged as e:
                        slowed.append(e)
                        return []
                    return [{**cm, "author": own if cm.pop("is_by_creator") else cm["author"]} for cm in got]
                await capture(conn, "pat", cands, _fetch, account_id=aid, own_author=own)
            except Exception as ce:  # noqa: BLE001 — capture must never fail a poll
                logger.warning("pat inbox capture failed: %s", type(ce).__name__)
            if slowed:
                raise slowed[0]

        await fa_standdown.reset("pat")
        if not is_first:
            try:
                await notifications.send_activity_alert("pat", activity, settings_key="pat_notifications_enabled")
            except Exception:  # noqa: BLE001
                logger.debug("pat activity alert failed", exc_info=True)
        duration = time.time() - start
        status = "partial" if partial else "success"
        pat_queries.finish_pat_poll_log(conn, log_id, status, duration_seconds=duration,
                                        error_message=partial,
                                        submissions_found=stats["submissions_found"],
                                        snapshots_inserted=stats["snapshots_inserted"])
        _progress("complete", message=f"Done -- {len(posts)} posts in {duration:.1f}s")
        return {**stats, **({"supporters": partial} if partial else {})}
    except Exception as e:
        if isinstance(e, PatChallenged):
            fa_standdown.note_challenge("pat")
            e = PatChallenged(f"{e} Next try around {fa_standdown.next_try_text('pat')}.")
        _progress("error", message=describe_error(e))
        logger.error("pat poll failed: %s", describe_error(e))
        if conn is not None and log_id is not None:
            pat_queries.finish_pat_poll_log(conn, log_id, "error", error_message=describe_error(e),
                                            duration_seconds=time.time() - start,
                                            submissions_found=stats["submissions_found"],
                                            snapshots_inserted=stats["snapshots_inserted"])
            conn.commit()
        raise e from None
    finally:
        _first_done.add(aid)
        _lock.release()
        if conn is not None:
            conn.close()
