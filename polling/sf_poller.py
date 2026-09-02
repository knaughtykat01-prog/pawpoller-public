"""SoFurry (SF) poll cycle orchestration.

Mirrors the Weasyl poller pattern (polling/ws_poller.py) since SoFurry has
similar data availability: views, likes, and comment counts only.

Key differences:
  - Authentication via a Personal Access Token on SoFurry's official API (3.4.0)
  - Gallery listing comes from the official API; per-submission STATS do not exist
    there at all, so those are read from login-free JSON on sofurry.com
  - Submission IDs are alphanumeric strings (not integers)
  - No individual comment or fave-user tracking
"""

from __future__ import annotations
import atexit
import logging
import threading
import time
from datetime import datetime, timezone
from html import escape as _esc

import config
from clients.sf.client import SoFurryClient
from database.db import get_connection
from polling.notifications import describe_error
from database import sf_queries
from polling import notifications

logger = logging.getLogger(__name__)

# -- Progress tracking -------------------------------------------------
sf_poll_progress = {
    "active": False,
    "phase": "idle",
    "current": 0,
    "total": 0,
    "message": "",
}

_sf_poll_running = False
_sf_poll_lock = threading.Lock()
_sf_first_poll_done: set[int] = set()

# Persistent client — reused across poll cycles to avoid re-logging in
# every time.  Recreated only when credentials change in settings.
_sf_client: SoFurryClient | None = None


def _cleanup_sf_client():
    if _sf_client is not None:
        import asyncio
        try:
            asyncio.get_event_loop().run_until_complete(_sf_client.close())
        except Exception:
            logger.debug("Error alert send failed", exc_info=True)


atexit.register(_cleanup_sf_client)


def _update_sf_progress(phase: str, current: int = 0, total: int = 0, message: str = ""):
    sf_poll_progress["active"] = phase not in ("idle", "complete", "error")
    sf_poll_progress["phase"] = phase
    sf_poll_progress["current"] = current
    sf_poll_progress["total"] = total
    sf_poll_progress["message"] = message


def _send_sf_notifications(new_details: list[dict]) -> None:
    """Send Windows toast notifications for SoFurry activity.

    SF tracks aggregate stat changes (views, faves, comments combined)
    without distinguishing the change type, so ``sf_notification_comments_only``
    suppresses these generic alerts entirely. Follower notifications are
    unaffected — they use a separate code path.
    """
    settings = config.get_settings()
    if settings.get("sf_notification_comments_only", False):
        return
    n = len(new_details)
    notifications.maybe_show_toast(
        settings,
        "sf_notifications_enabled",
        f"SF: {n} Submission{'s' if n != 1 else ''} Updated",
        [f"{d['title']} gained activity" for d in new_details],
    )


async def _send_sf_telegram(new_details: list[dict]) -> None:
    """Send Telegram notification for SoFurry activity.

    Same ``sf_notification_comments_only`` filter as the toast path.
    """
    settings = config.get_settings()
    if settings.get("sf_notification_comments_only", False):
        return
    n = len(new_details)
    await notifications.maybe_send_telegram_summary(
        settings,
        f"<b>SF: {n} Submission{'s' if n != 1 else ''} Updated</b>",
        [_esc(d['title']) for d in new_details],
        log_label="SF",
    )


def _send_sf_follower_notifications(new_follower_names: list[str]) -> None:
    """Send Windows toast notifications for new SF followers."""
    settings = config.get_settings()
    n = len(new_follower_names)
    notifications.maybe_show_toast(
        settings,
        "sf_notifications_enabled",
        f"SF: {n} New Follower{'s' if n != 1 else ''}",
        [f"  {name}" for name in new_follower_names],
    )


async def _send_sf_follower_telegram(new_follower_names: list[str]) -> None:
    """Send Telegram notification for new SF followers."""
    settings = config.get_settings()
    n = len(new_follower_names)
    await notifications.maybe_send_telegram_summary(
        settings,
        f"<b>🐾 SF: {n} New Follower{'s' if n != 1 else ''}</b>",
        [_esc(name) for name in new_follower_names],
        log_label="SF follower",
    )


def _get_or_create_client(settings: dict, account_id: int, is_default: bool) -> SoFurryClient:
    """Return the persistent SoFurryClient, re-pointed at the account's creds.

    SF accounts poll sequentially (single lock), so one persistent client whose
    credentials are updated each cycle suffices. Session cookies are stored and
    cleared under the account's own settings key.
    """
    global _sf_client
    creds = config.resolve_account_credentials("sf", account_id, is_default, settings)
    sf_token = creds.get("sf_api_token", "")
    sf_display = creds.get("sf_display_name", "")

    from polling.cf_proxy import proxy_kwargs
    sf_proxy = proxy_kwargs(settings, "sf")

    if _sf_client is None:
        _sf_client = SoFurryClient(
            api_token=sf_token,
            display_name=sf_display,
            **sf_proxy,
        )
    else:
        _sf_client.update_credentials(sf_token, sf_display)

    return _sf_client


async def run_sf_poll_cycle(account_id: int | None = None, force_full: bool = False) -> dict:
    """Execute one complete SoFurry poll cycle for a single account.

    Steps:
      1. Login and validate session
      2. Discover all gallery submissions
      3. Fetch details for each submission
      4. Upsert submissions and record snapshots
    """
    global _sf_poll_running

    from database import accounts as accounts_db
    _ac = get_connection()
    try:
        if account_id is None:
            account_id = accounts_db.get_default_account_id(_ac, "sf", create=True)
        account_row = accounts_db.get_account(_ac, account_id)
    finally:
        _ac.close()
    is_default = bool(account_row["is_default"]) if account_row else True
    is_first = account_id not in _sf_first_poll_done

    if not _sf_poll_lock.acquire(blocking=False):
        logger.warning("SF poll already running -- skipping (account %s)", account_id)
        return {}
    _sf_poll_running = True
    _update_sf_progress("starting", message="Initialising SoFurry poll cycle...")

    conn = None
    log_id = None
    start_time = time.time()

    stats = {
        "submissions_found": 0,
        "snapshots_inserted": 0,
        "new_watchers_found": 0,
    }

    settings = config.get_settings()
    client = _get_or_create_client(settings, account_id, is_default)

    try:
        conn = get_connection()
        log_id = sf_queries.start_sf_poll_log(conn, account_id)
        # Step 1+2: list the gallery via the official API.
        # 3.4.0: this used to scrape /u/{handle}/gallery.data and hand the poller
        # unvalidated 8-char "candidate" ids, and an unauthenticated request there
        # was SFW-filtered so an adult gallery yielded nothing. The official
        # /v1/user/{handle}/submissions returns the real list, private works
        # included. Per-submission STATS still come from the login-free JSON
        # endpoint (the official API exposes none), so DB-known ids are still polled
        # regardless — a listing failure degrades discovery, never the whole cycle.
        _update_sf_progress("searching", message="Listing gallery via the SoFurry API...")
        _handle_before = client.display_name
        try:
            gallery = await client.get_all_gallery_ids()
        except Exception as list_err:
            logger.warning(
                "SF: gallery listing failed (%s) — polling DB-known ids only", list_err)
            gallery = []
        # The client self-heals a wrong handle mid-listing (see
        # get_all_gallery_ids). Persist the correction to THIS account's own key,
        # or it re-resolves on every cycle for ever — and write it per-account,
        # because the bare key belongs to the default account and clobbering it
        # is how the DeviantArt tokens died in 3.21.0.
        if client.display_name and client.display_name != _handle_before:
            _key = config.account_setting_key(account_id, "sf_display_name", is_default)
            config.save_settings({_key: client.display_name})
            logger.info("SF: stored corrected handle %r for account %s (%s)",
                        client.display_name, account_id, _key)
        # Skip the owner's PRIVATE/UNLISTED works. The official listing includes
        # them (that is the point of an authenticated listing), but stats are read
        # from the anonymous endpoint, which cannot see them — so polling one
        # yields an empty detail that the title guard below would report as a junk
        # id. There are no public stats to collect for an unpublished work anyway.
        # privacy: 1=Private, 2=Unlisted, 3=Public; 0/absent → poll it and let the
        # title guard decide, so an unexpected shape never silently drops a work.
        non_public = [s["submission_id"] for s in gallery
                      if s.get("privacy") in (1, 2)]
        discovered = [s["submission_id"] for s in gallery
                      if s["submission_id"] not in set(non_public)]
        # Scoped to THIS account (3.24.0). Unscoped, every SoFurry account
        # re-polled every OTHER account's submissions: with two accounts
        # configured, KnaughtyKat's cycle fetched all 17 of SecondFur's works
        # 1,462 times, and because `upsert_sf_submission` sets account_id on
        # INSERT only, whichever account inserted a row first owned it for good.
        # The result was a SoFurry Submissions page that showed nothing at all
        # when filtered to SecondFur — every one of his rows was stamped
        # KnaughtyKat. SF is the only poller that unions DB-known ids into its
        # poll list (FA and the rest poll strictly what their own gallery
        # listing returns), which is why it was the only one affected.
        known = [r["submission_id"] for r in
                 sf_queries.get_all_sf_submissions(conn, account_id=account_id)]
        submission_ids = list(dict.fromkeys(discovered + known))  # de-dup, keep order
        stats["submissions_found"] = len(submission_ids)
        if non_public:
            logger.info(
                "SF: skipping %d unpublished work(s) — private/unlisted submissions "
                "have no public stats to collect", len(non_public))
        logger.info("SF: %d submissions to poll (%d discovered, %d known)",
                    len(submission_ids), len(discovered), len(known))

        # (3.4.0: no session cookies to persist — a PAT never logs in.)

        if not submission_ids:
            _update_sf_progress("complete", message="No SoFurry submissions found.")
            sf_queries.finish_sf_poll_log(conn, log_id, "success",
                                          duration_seconds=time.time() - start_time, **stats)
            conn.commit()
            return stats

        # Step 3: Fetch details
        _update_sf_progress("fetching_details",
                            message=f"Fetching details for {len(submission_ids)} submissions...")
        details = await client.get_submission_details_batch(submission_ids)
        logger.info("SF: Fetched details for %d submissions", len(details))

        # Step 4: Upsert + snapshot
        poll_timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        for idx, detail in enumerate(details, 1):
            _update_sf_progress("processing", current=idx, total=len(details),
                                message=f"Processing submission {idx}/{len(details)}...")
            try:
                sub_id = detail["submission_id"]
                views = detail.get("views", 0)
                faves = detail.get("favorites_count", 0)

                # comments_count is None when the count could not be read (it comes
                # from a separate payload to views/likes — see the client). Writing 0
                # would look like every comment was deleted, and the next successful
                # poll would then re-report them all as new. Carry the previous value
                # forward instead.
                comments = detail.get("comments_count")
                if comments is None:
                    comments = sf_queries.get_sf_previous_comments_count(conn, sub_id) or 0
                    logger.debug("SF: comment count unavailable for %s — kept %d",
                                 sub_id, comments)
                # Write the resolved value BACK into detail: upsert_sf_submission
                # reads the dict, not this local, so leaving it None would store a
                # NULL comments_count on the submission row while the snapshot got
                # the right number. (Caught on the 3.4.0 prod poll — 2 NULL rows.)
                detail["comments_count"] = comments

                # Reject anything that doesn't resolve to a titled submission — it
                # must not be persisted as a junk 0-view row. Since 3.4.0 the ids
                # come from the authoritative API listing rather than a token
                # scrape, so this should now only fire for a work the anonymous
                # stats endpoint can't read (deleted, or made private between the
                # listing and this fetch). Known works keep their row; the views==0
                # guard below handles their transient failures.
                if not (detail.get("title") or "").strip():
                    if not sf_queries.get_sf_submission(conn, sub_id):
                        logger.warning(
                            "SF: skipping %s — no public detail returned "
                            "(deleted, or unpublished since the listing)",
                            sub_id,
                        )
                        continue

                # Skip transient fetch failures: SF views are cumulative and
                # never drop, so a fetched 0 when the DB already holds a non-zero
                # count means the stats request failed, not a real reset.
                # Persisting it would corrupt the baseline and inflate the next
                # digest/milestone delta (the AO3/FA zero-snapshot class of bug).
                if views == 0:
                    existing = sf_queries.get_sf_submission(conn, sub_id)
                    if existing and (existing.get("views") or 0) > 0:
                        logger.warning(
                            "SF: skipping %s — scraped 0 views but DB has %d "
                            "(transient fetch failure, not a reset)",
                            sub_id, existing["views"],
                        )
                        continue

                sf_queries.upsert_sf_submission(conn, detail, account_id)
                sf_queries.insert_sf_snapshot(conn, account_id, sub_id, views, faves, comments,
                                              polled_at=poll_timestamp)
                stats["snapshots_inserted"] += 1

            except Exception as e:
                logger.warning("Error processing SF submission %s: %s",
                               detail.get("submission_id"), e, exc_info=True)

        conn.commit()

        # ── Step 5: Scrape followers ────────────────────────────
        new_follower_names: list[str] = []
        try:
            _update_sf_progress("fetching_watchers", message="Scraping follower list...")
            followers = await client.scrape_followers()
            for username in followers:
                is_new = sf_queries.upsert_sf_watcher(conn, account_id, username)
                if is_new:
                    stats["new_watchers_found"] += 1
                    new_follower_names.append(username)
            # Prune followers no longer on the live list
            if followers:
                removed = sf_queries.remove_stale_sf_watchers(conn, account_id, followers)
                if removed:
                    logger.info("SF: pruned %d stale followers from DB", removed)
            conn.commit()
        except Exception as we:
            logger.warning("Failed to scrape SF followers: %s", we, exc_info=True)

        # ── Notifications (followers) ────────────────────────────
        if is_first:
            logger.info("First SF poll for account %s -- suppressing %d follower notifications",
                        account_id, len(new_follower_names))
        else:
            if new_follower_names:
                try:
                    _send_sf_follower_notifications(new_follower_names)
                except Exception as ne:
                    logger.warning("Failed to send SF follower notifications: %s", ne, exc_info=True)
                try:
                    await _send_sf_follower_telegram(new_follower_names)
                except Exception as te:
                    logger.warning("Failed to send SF follower Telegram notification: %s", te, exc_info=True)

        # Finalise
        duration = time.time() - start_time
        _update_sf_progress("complete", current=len(details), total=len(details),
                            message=f"Done -- {stats['submissions_found']} submissions, {stats['new_watchers_found']} new followers in {duration:.1f}s")
        sf_queries.finish_sf_poll_log(conn, log_id, "success",
                                      duration_seconds=duration, **stats)
        logger.info("SF poll complete in %.1fs -- %d submissions, %d snapshots, %d new followers",
                     duration, stats["submissions_found"], stats["snapshots_inserted"],
                     stats["new_watchers_found"])

        # ── Telegram notifications ────────────────────────────
        if not is_first:
            from polling.telegram import send_poll_summary, check_milestones_batch, check_goals
            try:
                await send_poll_summary("sf", stats, duration)
            except Exception as te:
                logger.warning("Failed to send SF Telegram summary: %s", te, exc_info=True)
            try:
                await check_milestones_batch("sf", "sf_snapshots", "sf_submissions", account_id)
            except Exception as me:
                logger.warning("Failed to check SF milestones: %s", me, exc_info=True)
            try:
                await check_goals()
            except Exception as ge:
                logger.warning("Failed to check goals: %s", ge, exc_info=True)

        return stats

    except Exception as e:
        duration = time.time() - start_time
        _update_sf_progress("error", message=describe_error(e))
        logger.error("SF poll failed: %s", describe_error(e), exc_info=True)
        if conn and log_id:
            sf_queries.finish_sf_poll_log(conn, log_id, "error",
                                          error_message=describe_error(e),
                                          duration_seconds=duration, **stats)
            conn.commit()
        # Send error alert via Telegram
        from polling.telegram import send_poll_error
        try:
            await send_poll_error("sf", e)
        except Exception:
            logger.debug("Error alert send failed", exc_info=True)
        raise
    finally:
        _sf_first_poll_done.add(account_id)
        _sf_poll_running = False
        _sf_poll_lock.release()
        # NOTE: client is NOT closed here — it persists across poll cycles
        # to reuse the authenticated session and avoid re-logging in.
        if conn:
            conn.close()
