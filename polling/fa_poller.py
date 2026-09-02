"""FurAffinity (FA) poll cycle orchestration.

Mirrors the Inkbunny poller pattern (see polling/poller.py) with two key
differences driven by FA's data-access constraints:

  1. **No faving-user tracking** -- FurAffinity does not expose per-submission
     fave lists through FAExport or its public pages, so the FA poller has
     no step 5 equivalent.  The ``stats`` dict omits ``new_faves_found``
     entirely.

  2. **Comment fetching via FAExport API** -- Instead of scraping raw HTML
     (as the IB poller does), comments are retrieved through the FAExport
     JSON endpoint.  This is more reliable but still rate-limited, so the
     same delta-based "only fetch when count changes" optimisation applies.

The rest of the structure (progress dict, concurrency guard, six-step
cycle, notification dispatch) is intentionally identical to the IB poller
so that the frontend can treat all platform pollers uniformly.
"""

from __future__ import annotations
import asyncio
import logging
import threading
import time
from datetime import datetime, timezone
from html import escape as _esc

import httpx

from polling import notifications
from polling import self_comment
from polling.notifications import describe_error

import config
from clients.fa.client import FAClient
from database.db import get_connection
from database import fa_queries

logger = logging.getLogger(__name__)


class FAExportUpstreamError(Exception):
    """FAExport (the third-party FA scraper) returned an error PawPoller cannot fix.

    Raised in place of the raw httpx exception so logs / dashboard / Telegram /
    DB error_message all show an actionable explanation instead of a confusing
    5xx URL dump that looks like a PawPoller bug when it isn't.
    """


def _humanize_fa_error(e: BaseException) -> BaseException:
    """Translate known upstream failures into a clearer exception.

    Returns the original exception unchanged if no pattern matches, so we never
    mask a genuine bug behind a generic message. FAExport (faexport.spangle.org.uk)
    is a third-party proxy maintained by Deer-Spangle — when it 5xxs, the issue
    is on their side (their session against FA expired, FA changed HTML, or FA
    is challenging their egress IP), not ours.
    """
    if isinstance(e, httpx.HTTPStatusError):
        try:
            status = e.response.status_code
            url = str(e.request.url)
        except Exception:
            return e
        if "faexport." in url:
            if 500 <= status < 600:
                return FAExportUpstreamError(
                    f"FAExport upstream error ({status}) — third-party proxy "
                    "faexport.spangle.org.uk could not fetch data from "
                    "FurAffinity. Not a PawPoller bug; will retry next cycle. "
                    "If it persists for more than a day, see "
                    "https://github.com/Deer-Spangle/faexport/issues."
                )
            if status == 429:
                return FAExportUpstreamError(
                    "FAExport rate-limited (429) — shared-bucket pressure "
                    "across all FAExport users. Will retry next cycle."
                )
            if status == 404:
                return FAExportUpstreamError(
                    f"FAExport returned 404 for {url} — FA username may be "
                    "wrong or the account was removed from FurAffinity."
                )
    return e

# ── Progress tracking ────────────────────────────────────────
# Shared mutable dict read by /api/fa/poll/progress -- same pattern as
# the IB poller's poll_progress.  Prefixed with ``fa_`` so the two dicts
# can coexist at module level without collision.
fa_poll_progress = {
    "active": False,
    "phase": "idle",
    "current": 0,
    "total": 0,
    "message": "",
}

# Concurrency guard -- same purpose as _poll_running in the IB poller.
# Prevents overlapping FA poll cycles.  The Lock protects the
# check-and-set from race conditions; the boolean remains as a
# readable status indicator.
_fa_poll_running = False
_fa_poll_lock = threading.Lock()

# First-poll suppression is PER ACCOUNT (silent baseline on each account's first
# poll). Tracks which accounts have completed a cycle. A single lock still
# serialises all FA polls — accounts on one platform poll sequentially.
_fa_first_poll_done: set[int] = set()

# ── Watcher spam filter ──────────────────────────────────────
# FA attracts waves of bot/spam watchers with obvious patterns.
# This filter suppresses notifications (not DB storage) for them.
import re

# Why the spam keyword filter exists:
# FA has a persistent problem with bot accounts whose usernames contain
# gambling, adult-service, or crypto keywords.  These watchers are stored in
# the DB for completeness (accurate watcher counts), but notifications are
# suppressed to avoid alert fatigue from obvious spam.
_SPAM_KEYWORDS = re.compile(
    r"(1xbet|promo|casino|betting|slot|poker|viagra|cialis|crypto|forex|"
    r"onlyfans|escort|dating|hookup|webcam|livecam|sexchat|porno)",
    re.IGNORECASE,
)
# Alphanumeric soup: mostly digits with a few letters, or long gibberish
# e.g. "2charlottec262ye0", "123gaa", "a8k3m2x9p1"
_ALPHANUM_SOUP = re.compile(r"^(?=.*\d)[a-z0-9]{8,}$", re.IGNORECASE)

# Bulk threshold: if more than this many new watchers in one cycle,
# it's almost certainly a spam wave — summarise instead of listing names.
_SPAM_WAVE_THRESHOLD = 20


def _is_spam_watcher(username: str) -> bool:
    """Heuristic check for bot/spam watcher usernames."""
    if _SPAM_KEYWORDS.search(username):
        return True
    # Alternating letters+digits pattern (e.g. "a8k3m2x9p1")
    if _ALPHANUM_SOUP.match(username):
        digit_ratio = sum(c.isdigit() for c in username) / len(username)
        if digit_ratio >= 0.4:
            return True
    return False


def _update_fa_progress(phase: str, current: int = 0, total: int = 0, message: str = ""):
    """Mutate the shared fa_poll_progress dict so the frontend can display
    real-time status.  Mirrors _update_progress() in the IB poller."""
    fa_poll_progress["active"] = phase not in ("idle", "complete", "error")
    fa_poll_progress["phase"] = phase
    fa_poll_progress["current"] = current
    fa_poll_progress["total"] = total
    fa_poll_progress["message"] = message


def _send_fa_notifications(new_comment_details: list[dict],
                           new_watcher_names: list[str] | None = None) -> None:
    """Send Windows toast notifications for new FA comments and watchers.

    Unlike the IB poller, this does not handle faves — FA doesn't expose
    per-submission fave lists. Comments and watchers are emitted as two
    separate toasts so the user can distinguish at a glance.
    """
    if new_watcher_names is None:
        new_watcher_names = []

    settings = config.get_settings()
    notifications.maybe_show_toast(
        settings,
        "fa_notifications_enabled",
        f"FA: {len(new_comment_details)} New Comment"
        f"{'s' if len(new_comment_details) != 1 else ''}",
        [f"{d['username']} commented on {d['title']}" for d in new_comment_details],
    )
    # Watchers gated on a second per-platform flag.
    if settings.get("fa_watcher_notifications_enabled", True):
        notifications.maybe_show_toast(
            settings,
            "fa_notifications_enabled",
            f"FA: {len(new_watcher_names)} New Watcher"
            f"{'s' if len(new_watcher_names) != 1 else ''}",
            [f"{name} started watching you" for name in new_watcher_names],
        )


async def _send_fa_telegram(new_comment_details: list[dict],
                            new_watcher_names: list[str] | None = None,
                            account_id: int | None = None) -> None:
    """Send Telegram notification for new FA comments and watchers.

    Combined into one message (with a blank-line separator) when both
    sections present, so the chat doesn't get two pings for one cycle.
    With multiple FA accounts, the message leads with a persona/account line.
    """
    if new_watcher_names is None:
        new_watcher_names = []

    settings = config.get_settings()
    if not settings.get("telegram_enabled", False):
        return
    token = settings.get("telegram_bot_token")
    chat_id = settings.get("telegram_chat_id")
    if not token or not chat_id:
        return
    if not new_comment_details and not new_watcher_names:
        return

    sections: list[str] = []
    if new_comment_details:
        sections.append(notifications.format_telegram_summary(
            f"<b>🦊 FA: {len(new_comment_details)} New Comment"
            f"{'s' if len(new_comment_details) != 1 else ''}</b>",
            [f"<b>{_esc(d['username'])}</b> commented on {_esc(d['title'])}"
             for d in new_comment_details],
        ))
    if new_watcher_names and settings.get("fa_watcher_notifications_enabled", True):
        sections.append(notifications.format_telegram_summary(
            f"<b>🦊 FA: {len(new_watcher_names)} New Watcher"
            f"{'s' if len(new_watcher_names) != 1 else ''}</b>",
            [f"<b>{_esc(name)}</b> started watching" for name in new_watcher_names],
        ))
    if not sections:
        return
    body = "\n\n".join(sections)
    from polling.telegram import account_alert_prefix
    prefix = account_alert_prefix("fa", account_id)
    if prefix:
        body = f"🦊 <b>{prefix[:-3]}</b>\n\n{body}"   # strip the trailing " — "
    await notifications.send_telegram(
        token, chat_id, body, log_label="FA",
    )


async def run_fa_poll_cycle(account_id: int | None = None, force_full: bool = False) -> dict:
    """Execute one complete FurAffinity poll cycle for a single account.

    Follows the same pattern as the IB poller (run_poll_cycle) but with a
    reduced step count because FA's data model is more limited:

      1. **Gallery discovery** -- fetch all submission IDs via FAExport.
         (No auth step needed here; FAExport uses cookies set on the client.)
      2. **Detail fetch**      -- batch-fetch metadata for each submission.
      3. **Upsert + snapshot** -- write/update submission rows and record
                                  point-in-time stats.
      4. **Comments**          -- fetch comments via the FAExport API when
                                  the comment count has changed (or force_full).

    There is **no faving-user step** because FurAffinity does not expose
    per-submission fave lists through FAExport or any public endpoint.
    The stats dict therefore has no ``new_faves_found`` key.

    Args:
        force_full: When True, re-fetch comments for every submission
            regardless of whether their counts changed.

    Returns:
        Stats dict with keys: submissions_found, snapshots_inserted,
        new_comments_found.  Empty dict if a poll was already running.
    """
    global _fa_poll_running

    # ── Resolve the account to poll (default FA account when unspecified) ──
    from database import accounts as accounts_db
    _ac = get_connection()
    try:
        if account_id is None:
            account_id = accounts_db.get_default_account_id(_ac, "fa", create=True)
        account_row = accounts_db.get_account(_ac, account_id)
    finally:
        _ac.close()
    is_default = bool(account_row["is_default"]) if account_row else True
    is_first = account_id not in _fa_first_poll_done

    # Concurrency guard -- one FA poll cycle at a time (accounts poll sequentially).
    if not _fa_poll_lock.acquire(blocking=False):
        logger.warning("FA poll already running — skipping (account %s)", account_id)
        return {}
    _fa_poll_running = True
    _update_fa_progress("starting", message="Initialising FA poll cycle...")

    conn = None
    log_id = None
    start_time = time.time()

    # Note: no "new_faves_found" key -- FA doesn't provide faving user data.
    stats = {
        "submissions_found": 0,
        "snapshots_inserted": 0,
        "new_comments_found": 0,
        "new_watchers_found": 0,
    }

    # FA authentication uses cookie_a / cookie_b rather than a session ID.
    # Resolve this account's credentials (default → legacy flat keys; extra
    # accounts → namespaced keys) and pass them to the FAClient.
    settings = config.get_settings()
    creds = config.resolve_account_credentials("fa", account_id, is_default, settings)
    from polling.cf_proxy import proxy_kwargs
    client = FAClient(
        username=creds.get("fa_username", ""),
        cookie_a=creds.get("fa_cookie_a", ""),
        cookie_b=creds.get("fa_cookie_b", ""),
        **proxy_kwargs(settings, "fa"),
    )
    # Our own handle(s), for excluding self-comments from "new comment" counts
    # (2.192.0). FA has always known its username — it just never used it here.
    my_handles = ({self_comment.normalise_handle(creds.get("fa_username", ""))}
                  if creds.get("fa_username") else set())

    try:
        conn = get_connection()
        log_id = fa_queries.start_fa_poll_log(conn, account_id)
        # ── Step 1: Discover gallery submissions ──────────────
        # Primary source is FAExport (JSON proxy). When it's unavailable (the
        # long Cloudflare block on faexport.spangle.org.uk — faexport#129) we
        # fall back to scraping FA's gallery/submission HTML directly via the
        # user's cookies. Direct mode only works from a residential IP (the
        # desktop instance) — FA's Cloudflare blocks the datacenter server IP,
        # the same constraint as FA posting. Set fa_direct_polling=true to skip
        # FAExport entirely (recommended while it stays blocked).
        _update_fa_progress("searching", message="Validating FA cookies...")
        if not await client.validate_cookies():
            raise ValueError("FA cookies expired — update cookie_a and cookie_b in Settings")

        use_direct = bool(settings.get("fa_direct_polling", False))
        if use_direct:
            _update_fa_progress("searching", message="Scraping gallery directly from FA...")
            gallery = await client.get_all_gallery_ids_direct()
        else:
            _update_fa_progress("searching", message="Fetching gallery from FAExport...")
            try:
                gallery = await client.get_all_gallery_ids()
            except Exception as ge:  # noqa: BLE001 — FAExport down → try direct FA
                logger.warning("FAExport gallery failed (%s) — falling back to direct FA scraping",
                               describe_error(ge))
                use_direct = True
                _update_fa_progress("searching", message="FAExport down — scraping FA directly...")
                gallery = await client.get_all_gallery_ids_direct()
        submission_ids = [s["submission_id"] for s in gallery]
        stats["submissions_found"] = len(submission_ids)
        logger.info("FA: Found %d submissions (%s)", len(submission_ids),
                    "direct" if use_direct else "FAExport")

        if not submission_ids:
            _update_fa_progress("complete", message="No FA submissions found.")
            fa_queries.finish_fa_poll_log(conn, log_id, "success", duration_seconds=time.time() - start_time, **stats)
            conn.commit()
            return stats

        # ── Step 2: Fetch details for each submission ──────────
        _update_fa_progress("fetching_details", message=f"Fetching details for {len(submission_ids)} submissions...")
        if use_direct:
            details = await client.get_submission_details_batch_direct(submission_ids)
        else:
            details = await client.get_submission_details_batch(submission_ids)
        logger.info("FA: Fetched details for %d submissions", len(details))

        # ── Step 3 & 4: Upsert + snapshot, then conditional comments ──
        new_comment_details = []
        poll_timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        for idx, detail in enumerate(details, 1):
            _update_fa_progress("processing", current=idx, total=len(details),
                                message=f"Processing submission {idx}/{len(details)}...")

            # Per-submission try/except -- same resilience pattern as IB.
            try:
                sub_id = detail["submission_id"]
                views = detail.get("views", 0)
                faves = detail.get("favorites_count", 0)
                comments = detail.get("comments_count", 0)

                # Defence against a transient scrape failure writing a 0-view
                # snapshot. FA views are cumulative and never drop, so a scraped
                # 0 when the DB already holds a non-zero count means the fetch
                # failed — most often a Cloudflare challenge page that returns
                # HTTP 200 and parses to all-zero stats (now an expected event
                # under FA's third-party policy / DDoS-mitigation guidance).
                # Persisting it corrupts the baseline and inflates the next
                # digest/milestone delta (same class as the AO3 bug, [2.27.1]).
                # Skip the work this cycle; the next one re-reads the truth.
                if views == 0:
                    existing = fa_queries.get_fa_submission(conn, sub_id)
                    if existing and (existing.get("views") or 0) > 0:
                        logger.warning(
                            "FA: skipping submission %s — scraped 0 views but DB "
                            "has %d (transient fetch/challenge failure, not a reset)",
                            sub_id, existing["views"],
                        )
                        continue

                # Grab the previous comment count *before* the snapshot
                # overwrites it -- needed for the delta check below.
                prev_comments = fa_queries.get_fa_previous_comments_count(conn, sub_id)

                # Step 3: Upsert submission and record snapshot.
                fa_queries.upsert_fa_submission(conn, detail, account_id)
                fa_queries.insert_fa_snapshot(conn, account_id, sub_id, views, faves, comments, polled_at=poll_timestamp)
                stats["snapshots_inserted"] += 1
                # Commit before the conditional comment fetch below: holding
                # the implicit write transaction across its awaits blocks
                # every other poller's writes past the 30s busy_timeout.
                conn.commit()

                # ── Step 4: Fetch comments (conditional) ───────
                # Uses the FAExport /submission/{id}.json endpoint to get
                # comments as structured JSON, unlike the IB poller which
                # scrapes HTML.  Same delta-based optimisation: only fetch
                # when count has changed or on force_full.
                # Comment scraping uses FAExport; skip it in direct mode (the
                # snapshot already captured comments_count from the page).
                should_fetch_comments = (not use_direct) and force_full and comments > 0
                if not should_fetch_comments and not use_direct:
                    if (prev_comments is not None and comments > prev_comments) or \
                       (prev_comments is None and comments > 0):
                        should_fetch_comments = True

                if should_fetch_comments:
                    logger.info("FA submission %d: fetching comments (count=%d, force=%s)", sub_id, comments, force_full)
                    # Rate-limit delay before each comment fetch.
                    await asyncio.sleep(config.FA_REQUEST_DELAY_SECONDS)
                    try:
                        scraped = await client.get_submission_comments(sub_id)
                        # Batch insert: get existing comment_ids first to identify new ones
                        existing_cids = {r["comment_id"] for r in fa_queries.get_fa_comments(conn, sub_id)}
                        fa_queries.upsert_fa_comments_batch(conn, account_id, scraped, my_handles)
                        conn.commit()
                        # Count and announce only comments that are NOT ours
                        # (2.192.0). The batch insert's total_changes cannot tell
                        # our rows apart, so the tally is derived here from the
                        # same is-own test the insert used.
                        for c in scraped:
                            if str(c["comment_id"]) in existing_cids:
                                continue
                            if self_comment.is_own_author(c.get("username", ""), my_handles):
                                continue
                            stats["new_comments_found"] += 1
                            new_comment_details.append({
                                "username": c.get("username", ""),
                                "title": detail.get("title", ""),
                            })
                    except Exception as ce:
                        # Comment fetch failure is non-fatal.
                        logger.warning("Failed to fetch FA comments for %d: %s", sub_id, ce, exc_info=True)

            except Exception as e:
                # Per-submission error: log and continue with the next one.
                logger.warning("Error processing FA submission %s: %s", detail.get("submission_id"), e, exc_info=True)

        conn.commit()

        # ── Step 5: Fetch watchers (confirmation delay + spam protection) ──
        #
        # Why watchers start as "pending" and need 2 cycles to confirm:
        # FA attracts waves of spam/bot watchers that appear briefly then vanish.
        # By requiring a watcher to be present in 2 consecutive polls, we filter
        # out ephemeral bots without false-positiving on real users who simply
        # haven't been scraped yet.  Only confirmed watchers trigger notifications.
        #
        # Flow:
        #   a) Upsert all watchers from FAExport (new ones start as pending/unconfirmed)
        #   b) Confirm pending watchers that were seen again (survived 2+ cycles)
        #   c) Profile-sniff newly confirmed watchers to catch bots with zero activity
        #   d) Keyword-filter remaining watchers
        #   e) Notify only confirmed, non-spam, non-notified watchers
        #
        new_watcher_names = []
        confirmed_watcher_names = []
        try:
            # Watcher list comes from FAExport — unavailable in direct mode, so
            # we skip the scrape (and the stale-prune, which only runs when we
            # actually fetched a list).
            _update_fa_progress("fetching_watchers", message="Fetching watcher list...")
            watchers = [] if use_direct else await client.get_all_watchers()
            for username in watchers:
                is_new = fa_queries.upsert_fa_watcher(conn, account_id, username)
                if is_new:
                    stats["new_watchers_found"] += 1
                    new_watcher_names.append(username)
            # Remove watchers no longer on the live list (banned/deleted/unwatched)
            if watchers:
                removed = fa_queries.remove_stale_fa_watchers(conn, account_id, watchers)
                if removed:
                    logger.info("FA: pruned %d stale watchers from DB", removed)
            conn.commit()

            if new_watcher_names:
                logger.info("FA: %d new watchers discovered (pending confirmation)", len(new_watcher_names))
                # Keyword-filter obvious spam immediately and mark in DB
                keyword_spam = [n for n in new_watcher_names if _is_spam_watcher(n)]
                if keyword_spam:
                    fa_queries.mark_watchers_spam(conn, account_id, keyword_spam)
                    conn.commit()
                    logger.info("FA watcher keyword filter: %d/%d flagged as obvious bots (e.g. %s)",
                                len(keyword_spam), len(new_watcher_names), ", ".join(keyword_spam[:3]))

            # Confirm pending watchers that survived from a previous cycle
            confirmed_watcher_names = fa_queries.confirm_pending_watchers(conn, account_id)
            conn.commit()

            if confirmed_watcher_names:
                logger.info("FA: %d watchers confirmed (seen in 2+ consecutive polls)", len(confirmed_watcher_names))

                # Profile sniff confirmed watchers to catch zero-activity bots.
                # Only sniff watchers not already flagged by keyword filter.
                # Cap at 10 to avoid excessive FAExport requests.
                to_sniff = [n for n in confirmed_watcher_names if not _is_spam_watcher(n)][:10]
                if to_sniff:
                    _update_fa_progress("sniffing_profiles", message=f"Checking {len(to_sniff)} watcher profiles...")
                    try:
                        sniff_results = await client.sniff_watcher_profiles(to_sniff)
                        profile_spam = [name for name, is_spam in sniff_results.items() if is_spam]
                        if profile_spam:
                            fa_queries.mark_watchers_spam(conn, account_id, profile_spam)
                            conn.commit()
                            logger.info("FA profile sniff: %d/%d confirmed watchers flagged as bots (zero activity)",
                                        len(profile_spam), len(to_sniff))
                    except Exception as pe:
                        logger.warning("FA profile sniff failed (non-fatal): %s", pe, exc_info=True)

        except Exception as we:
            logger.warning("Failed to fetch FA watchers: %s", we, exc_info=True)

        # ── Step 6: Fetch profile pageviews ───────────────────────
        # FAExport's /user/{name}.json returns a "pageviews" field representing
        # how many times the user's profile page has been visited. We snapshot
        # this value each poll cycle for historical charting.
        try:
            # Profile pageviews come from FAExport — skip in direct mode.
            _update_fa_progress("fetching_profile", message="Fetching profile stats...")
            profile = None if use_direct else await client.get_user_profile(client.username)
            if profile and "pageviews" in profile:
                from clients.fa.client import _safe_int
                pv = _safe_int(profile["pageviews"])
                fa_queries.insert_fa_profile_stats(conn, account_id, pv, polled_at=poll_timestamp)
                conn.commit()
                logger.info("FA: Profile pageviews recorded: %d", pv)
        except Exception as pe:
            logger.warning("Failed to fetch FA profile stats: %s", pe, exc_info=True)

        # ── Notifications (comments + confirmed watchers) ───────────
        # Skip on first poll after startup (silent baseline).
        # Watcher notifications respect fa_watcher_notification_mode:
        #   "immediate" (default) = notify per-poll as watchers confirm
        #   "daily"               = accumulate, sent via send_fa_watcher_digest()
        #   "off"                 = never notify about watchers
        settings = config.get_settings()
        watcher_mode = settings.get("fa_watcher_notification_mode", "immediate")
        notify_watchers = []
        if not is_first and watcher_mode == "immediate":
            notify_watchers = fa_queries.get_unnotified_confirmed_watchers(conn, account_id)

        if is_first:
            logger.info("First FA poll for account %s — suppressing %d comment, %d watcher notifications",
                        account_id, len(new_comment_details), len(new_watcher_names))
        else:
            # Comments always notify immediately; watchers depend on mode
            try:
                _send_fa_notifications(new_comment_details, notify_watchers)
            except Exception as ne:
                logger.warning("Failed to send FA notifications: %s", ne, exc_info=True)

            try:
                await _send_fa_telegram(new_comment_details, notify_watchers, account_id)
            except Exception as te:
                logger.warning("Failed to send FA Telegram notification: %s", te, exc_info=True)

            # Mark as notified so we don't re-send
            if notify_watchers:
                fa_queries.mark_watchers_notified(conn, account_id, notify_watchers)
                conn.commit()

        # ── Finalise ───────────────────────────────────────────
        duration = time.time() - start_time
        _update_fa_progress("complete", current=len(details), total=len(details),
                            message=f"Done — {stats['submissions_found']} submissions in {duration:.1f}s")
        fa_queries.finish_fa_poll_log(conn, log_id, "success", duration_seconds=duration, **stats)
        logger.info("FA poll complete in %.1fs — %d submissions, %d snapshots, %d new comments, %d new watchers",
                     duration, stats["submissions_found"], stats["snapshots_inserted"],
                     stats["new_comments_found"], stats["new_watchers_found"])

        # ── Telegram summaries + milestones ───────────────────
        from polling.telegram import send_poll_summary, check_milestones_batch, check_goals
        if not is_first:
            try:
                await send_poll_summary("fa", stats, duration)
            except Exception as te:
                logger.warning("Failed to send FA Telegram summary: %s", te, exc_info=True)
            try:
                await check_milestones_batch("fa", "fa_snapshots", "fa_submissions", account_id)
            except Exception as me:
                logger.warning("Failed to check FA milestones: %s", me, exc_info=True)
            try:
                await check_goals()
            except Exception as ge:
                logger.warning("Failed to check goals: %s", ge, exc_info=True)

        return stats

    except Exception as e:
        # Top-level failure -- record partial stats and propagate.
        # Translate known upstream patterns (FAExport 5xx/429/404) into clearer
        # messages before they reach the dashboard / Telegram / poll-log table.
        duration = time.time() - start_time
        friendly = _humanize_fa_error(e)
        friendly_msg = describe_error(friendly)
        _update_fa_progress("error", message=friendly_msg)
        logger.error("FA poll failed: %s", friendly_msg, exc_info=True)
        if conn and log_id:
            fa_queries.finish_fa_poll_log(conn, log_id, "error", error_message=friendly_msg, duration_seconds=duration, **stats)
            conn.commit()
        # Send error alert via Telegram
        from polling.telegram import send_poll_error
        try:
            await send_poll_error("fa", friendly)
        except Exception:
            logger.debug("Error alert send failed", exc_info=True)
        if friendly is e:
            raise
        raise friendly from e
    finally:
        # Always clear the guard and release resources. Mark this account's
        # first poll as done so a failed first attempt doesn't suppress
        # notifications on the next successful poll.
        _fa_first_poll_done.add(account_id)
        _fa_poll_running = False
        _fa_poll_lock.release()
        await client.close()
        if conn:
            conn.close()


async def send_fa_watcher_digest() -> None:
    """Send a daily digest of confirmed watchers that haven't been notified yet.

    Called by the digest scheduler when fa_watcher_notification_mode is "daily".
    Collects all unnotified confirmed non-spam watchers and sends a single
    Telegram message summarising them.
    """
    settings = config.get_settings()
    if settings.get("fa_watcher_notification_mode", "immediate") != "daily":
        return
    if not settings.get("telegram_enabled", False):
        return
    token = settings.get("telegram_bot_token")
    chat_id = settings.get("telegram_chat_id")
    if not token or not chat_id:
        return

    conn = get_connection()
    try:
        from database import accounts as accounts_db
        fa_accounts = accounts_db.list_accounts(conn, platform="fa", enabled_only=True)
        if not fa_accounts:
            default_id = accounts_db.get_default_account_id(conn, "fa")
            if default_id is not None:
                fa_accounts = [{"account_id": default_id, "label": "FA"}]

        # Collect this-cycle's pending watchers per account.
        per_account = []
        all_pending = []
        for a in fa_accounts:
            pending = fa_queries.get_unnotified_confirmed_watchers(conn, a["account_id"])
            if pending:
                per_account.append((a["account_id"], pending))
                all_pending.extend(pending)
        if not all_pending:
            return

        text = notifications.format_telegram_summary(
            f"<b>🦊 FA Daily Watcher Digest: {len(all_pending)} New Watcher"
            f"{'s' if len(all_pending) != 1 else ''}</b>",
            [f"<b>{_esc(name)}</b>" for name in all_pending],
            max_visible=10,
        )
        # Use the lower-level send_telegram primitive so we can branch on
        # delivery success — a failed send must NOT mark watchers notified
        # (otherwise tomorrow's digest would skip them).
        ok = await notifications.send_telegram(
            token, chat_id, text, log_label="FA digest",
        )
        if not ok:
            return

        for acct_id, pending in per_account:
            fa_queries.mark_watchers_notified(conn, acct_id, pending)
        conn.commit()
        logger.info("FA watcher digest sent: %d watchers across %d account(s)",
                    len(all_pending), len(per_account))
    finally:
        conn.close()
