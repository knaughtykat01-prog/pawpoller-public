"""Posting scheduler — daemon thread that processes the posting queue.

Runs as a background thread (like the pollers), checking the posting_queue
table every 60 seconds for pending items that are ready to process.

Items can be:
  - Immediate: scheduled_at is NULL → process on next check
  - Scheduled: scheduled_at is a future datetime → process when due
  - Retryable: failed items with attempts < max_attempts

The scheduler is started in main.py (desktop) and server.py (headless)
alongside the polling threads.
"""

from __future__ import annotations

import asyncio
import logging
import time

import config
from database.db import get_connection
from database import posting_queries
from posting import manager, story_reader

logger = logging.getLogger(__name__)

# How often to check the queue (seconds)
SCHEDULER_CHECK_INTERVAL = 60

# Runtime mode: set by the calling entry point (main.py or server.py)
_runtime_mode: str = "server"

# time.monotonic() at the top of each loop round (4.45.5) — /api/health/ready reads
# it to tell a stalled scheduler from a quiet one. 0.0 = the loop hasn't run yet.
LAST_TICK: float = 0.0


def _instance_id() -> str:
    """Who this process is, for the queue claim's ``claimed_by``.

    Mode alone isn't enough to attribute a stuck 'processing' row once more than
    one instance shares a queue — two desktops would be indistinguishable — so
    the hostname goes in too. Best-effort: a hostname lookup must never be able
    to stop a post going out.
    """
    try:
        import socket
        return f"{_runtime_mode}:{socket.gethostname()}"
    except Exception:
        return _runtime_mode


def detect_runtime_mode() -> str:
    """Detect whether we're running as desktop (main.py) or server (server.py).

    Desktop mode: pywebview is importable (main.py installs it).
    Server mode: pywebview is NOT available (requirements-server.txt excludes it).
    """
    try:
        import webview  # noqa: F401 — pywebview, desktop-only dependency
        return "desktop"
    except ImportError:
        return "server"


def start_posting_scheduler() -> None:
    """Entry point for the posting scheduler daemon thread.

    Creates its own asyncio event loop (standard pattern for PawPoller threads).
    Runs indefinitely, checking the queue on each iteration.

    Detects runtime mode (desktop/server) and only processes queue items whose
    'requires' field matches. Items requiring 'desktop' are skipped on the server
    and vice versa. Items with requires='any' are processed everywhere.
    """
    global _runtime_mode
    _runtime_mode = detect_runtime_mode()
    logger.info("Posting scheduler starting in %s mode", _runtime_mode)

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(_scheduler_loop())
    except Exception as e:
        logger.debug("Posting scheduler thread exiting: %s", e)


async def _scheduler_loop() -> None:
    """Main scheduler loop."""
    logger.info("Posting scheduler started (mode=%s)", _runtime_mode)

    # Brief startup delay to let other services initialize
    await asyncio.sleep(5)

    global LAST_TICK
    while True:
        LAST_TICK = time.monotonic()
        try:
            settings = config.get_settings()
            if not settings.get("posting_enabled", False):
                await asyncio.sleep(SCHEDULER_CHECK_INTERVAL)
                continue

            # Get next pending item that's compatible with our runtime mode.
            # The runtime_mode filter is applied in SQL so incompatible
            # items (e.g. requires='desktop' on a server instance) don't
            # block compatible ones at the head of the FIFO.
            conn = get_connection()
            try:
                items = posting_queries.get_pending_queue(
                    conn, limit=5, runtime_mode=_runtime_mode
                )
            finally:
                conn.close()

            if items:
                await _process_queue_item(items[0])
                # Brief pause between queue items to avoid busy-looping
                await asyncio.sleep(5)
            else:
                await asyncio.sleep(SCHEDULER_CHECK_INTERVAL)

        except Exception as e:
            logger.error("Posting scheduler error: %s", e, exc_info=True)
            await asyncio.sleep(SCHEDULER_CHECK_INTERVAL)


_slot_jobs: dict[str, str] = {}   # slot → activity job id (spec 017)


def _slot_job(item) -> str:
    """The activity job this row reports into. The rows of one slot — a piece scheduled to
    five sites, one chapter of a drip — fire seconds apart and share one job, so the pill shows
    one thing ("Sample Story — ch 3, 4 of 5 sites") rather than five flashes."""
    from posting import activity
    keys = item.keys()
    ctype = item["content_type"] if "content_type" in keys else "story"
    group = item["drip_group"] if "drip_group" in keys and item["drip_group"] else f"{ctype}:{item['story_name']}"
    slot = f"{group}@{item['scheduled_at'] if 'scheduled_at' in keys else ''}"
    jid = _slot_jobs.get(slot)
    if jid and activity.reopen(jid):
        return jid
    name = str(item["story_name"])
    if ctype == "post":
        title = (item["title_override"] if "title_override" in keys and item["title_override"] else f"Post {name}")
    else:
        title = name.replace("_", " ")
        if ctype == "story" and item["chapter_index"]:
            title += f" — ch {item['chapter_index']}"
    jid = activity.start("scheduled", f"{title} (scheduled)", [], ref={"queue": True})
    _slot_jobs[slot] = jid
    for old in list(_slot_jobs)[:-100]:     # bounded; old slots are long finished
        _slot_jobs.pop(old, None)
    return jid


async def _process_queue_item(item: dict) -> None:
    """Process one due queue row inside its slot's activity job (spec 017)."""
    from posting import activity
    jid = _slot_job(item)
    with activity.bound(jid):
        try:
            await _process_queue_item_core(item)
        finally:
            activity.finish(jid)


async def _process_queue_item_core(item: dict) -> None:
    """Process a single posting queue item."""
    queue_id = item["queue_id"]
    story_name = item["story_name"]
    chapter_index = item["chapter_index"]
    platform = item["platform"]
    action = item["action"]
    # account_id may be 0 on rows queued before multi-account — treat as
    # "default account" (None lets the manager resolve the platform default).
    account_id = item["account_id"] if "account_id" in item.keys() else None
    account_id = account_id or None
    # Artwork rows reuse this queue with content_type='artwork'; microblog
    # posts reuse it with content_type='post' (story_name = the post_id).
    # Older rows predate the column and are stories.
    content_type = item["content_type"] if "content_type" in item.keys() else "story"
    # posting_queries has persisted this column all along; nothing forwarded it
    # (publish_flow spec §10 Q6). A scheduled "this post only" Telegram text.
    _desc_override = item["description_override"] if "description_override" in item.keys() else None
    description_overrides = {platform: _desc_override} if _desc_override else None
    # Which render this row is for (4.34.0, VARSPLIT). Carried on the row rather than
    # re-derived, because re-deriving is wrong for an alternate render: rated the same
    # as the piece, it derives to "no variant" and the retry would post the primary.
    _vk = item["variant_key"] if "variant_key" in item.keys() else ""
    variant_overrides = {platform: _vk} if _vk else None
    # Rows never announce to Discord on their own (4.43.0 batches; 4.43.1 every artwork and
    # story row): a piece scheduled to five sites is five rows, and each announcing was five messages.
    # The piece is announced ONCE when its rows settle — _maybe_batch_announce (a batch,
    # announce 0/1) or _maybe_slot_announce (a plain schedule, announce NULL, follows the switch).
    row_discord = False

    # For posts the story_name is a bare post_id — use the snippet stashed in
    # title_override as the human label in Telegram notifications.
    notify_name = story_name
    if content_type == "post":
        snip = item["title_override"] if "title_override" in item.keys() else None
        notify_name = snip or f"post {story_name}"

    logger.info(
        "Processing queue item #%d: %s %s ch%d on %s (account %s, %s)",
        queue_id, action, story_name, chapter_index, platform, account_id, content_type,
    )

    # Claim it. Conditional on status='pending', so if another instance sharing
    # this queue got there first we stop here rather than posting it a second
    # time. Losing the claim is routine — log at debug and move on.
    conn = get_connection()
    try:
        claimed = posting_queries.claim_queue_item(conn, queue_id, _instance_id())
    finally:
        conn.close()
    if not claimed:
        logger.debug(
            "Queue item #%d was already claimed or cancelled — skipping", queue_id
        )
        return

    try:
        # A persona-first schedule is checked again when it comes due (4.43.1, BATCHOPEN):
        # a batch spread over weeks must not post as an account that has since moved to
        # another persona, been disabled or removed. Refused = failed + notified, no retry.
        _persona = item["persona_id"] if "persona_id" in item.keys() else None
        if _persona is not None:
            from database import personas as personas_db
            conn = get_connection()
            try:
                _perr = personas_db.persona_account_error(conn, platform, account_id, int(_persona))
            finally:
                conn.close()
            if _perr:
                raise ValueError(f"Refused when it came due: {_perr}")

        if content_type == "post":
            # Microblog post: story_name is the post_id; publish this one
            # platform via the Posts engine (records in post_publications).
            from posting import post_publisher
            try:
                post_id = int(story_name)
            except (TypeError, ValueError):
                raise ValueError(f"post queue row has a non-numeric id: {story_name!r}")
            results = await post_publisher.publish_post(
                post_id, [platform],
                {platform: account_id} if account_id else None,
            )
        elif action == "post" and content_type == "artwork":
            results = await manager.post_artwork(
                story_name, [platform],
                account_ids={platform: account_id} if account_id else None,
                description_overrides=description_overrides,
                variant_overrides=variant_overrides,
                announce_discord=row_discord,
            )
        elif action == "post":
            results = await manager.post_story(
                story_name, [platform], [chapter_index],
                account_ids={platform: account_id} if account_id else None,
                description_overrides=description_overrides,
                announce_discord=row_discord,
            )
        elif action == "update":
            results = await manager.update_story(
                story_name, [platform], [chapter_index],
                account_filter=account_id,
            )
        else:
            raise ValueError(f"Unknown action: {action}")

        # Check results
        if results and results[0].get("success"):
            conn = get_connection()
            try:
                if content_type == "post":
                    # Posts record their outcome in post_publications, not the
                    # story/artwork publications registry — no pub_id to link.
                    pub_id = None
                else:
                    # Find the pub_id that was created/updated
                    pub = posting_queries.get_publication_by_story(
                        conn, story_name, chapter_index, platform, account_id,
                        content_type=content_type,
                    )
                    pub_id = pub["pub_id"] if pub else None
                posting_queries.update_queue_status(
                    conn, queue_id, "completed", pub_id=pub_id
                )
            finally:
                conn.close()
            logger.info("Queue item #%d completed successfully", queue_id)

            # Send Telegram notification
            await _notify_completion(notify_name, chapter_index, platform, action, True,
                                     content_type=content_type)
        else:
            error = results[0].get("error", "Unknown error") if results else "No results"
            # 2.22.10c: manager.post_story / update_story already call
            # _schedule_retry on failure, which adds a NEW queue row with
            # proper backoff (60s / 300s / 1800s). Previously the scheduler
            # ALSO set this same row back to "pending" with no scheduled_at
            # bump, causing tight-loop reprocessing every 5 seconds and
            # burning the queue item's attempts/max_attempts counter in
            # under 30 seconds. The fix: trust the manager's retry_queued
            # signal. If a new row was already queued (or desktop fallback
            # was used), mark this row "failed" so the scheduler stops
            # picking it up. If neither happened (edge case), keep the
            # legacy inline retry path so the row doesn't silently die.
            retry_queued = bool(results[0].get("retry_queued")) if results else False
            queued_for_desktop = bool(results[0].get("queued_desktop")) if results else False
            handed_off = retry_queued or queued_for_desktop

            if handed_off:
                new_status = "failed"
                _adopt_handoff(item)
            else:
                new_status = "pending" if item["attempts"] < item["max_attempts"] else "failed"

            conn = get_connection()
            try:
                posting_queries.update_queue_status(
                    conn, queue_id, new_status, error=error
                )
            finally:
                conn.close()

            if handed_off:
                logger.info(
                    "Queue item #%d failed; handoff to %s — marking this row failed",
                    queue_id,
                    "retry queue" if retry_queued else "desktop queue",
                )
            elif new_status == "pending":
                logger.warning("Queue item #%d failed (attempt %d/%d), will retry: %s",
                               queue_id, item["attempts"], item["max_attempts"], error)
            else:
                logger.warning("Queue item #%d failed permanently: %s", queue_id, error)
                await _notify_completion(notify_name, chapter_index, platform, action, False, error,
                                         content_type=content_type)

    except Exception as e:
        conn = get_connection()
        try:
            posting_queries.update_queue_status(conn, queue_id, "failed", error=str(e))
        finally:
            conn.close()
        logger.error("Queue item #%d exception: %s", queue_id, e, exc_info=True)
        from posting import activity
        activity.line_done(platform, False, error=str(e))
        await _notify_completion(notify_name, chapter_index, platform, action, False, str(e),
                                 content_type=content_type)

    await _maybe_batch_announce(item)
    await _maybe_slot_announce(item)


def _adopt_handoff(item) -> None:
    """The retry / desktop row the manager just queued re-runs THIS row (4.43.1).

    The manager only knows the piece, site and account, so its row came back without the
    batch, the Discord choice, the persona and the this-post-only text — a retried batch
    row then announced on its own, and a retry of a plain schedule announced a second time
    from its own later slot. Copy them over, and point it at the root row it re-runs."""
    try:
        keys = item.keys()
        g = lambda k: item[k] if k in keys else None   # noqa: E731
        conn = get_connection()
        try:
            conn.execute(
                "UPDATE posting_queue SET drip_group = ?, announce = ?, persona_id = ?, retry_of = ?, "
                "title_override = COALESCE(title_override, ?), "
                "description_override = COALESCE(description_override, ?) "
                "WHERE queue_id = (SELECT MAX(queue_id) FROM posting_queue WHERE queue_id > ? "
                "AND story_name = ? AND chapter_index = ? AND platform = ? AND content_type = ? "
                "AND status = 'pending' AND retry_of IS NULL)",
                (g("drip_group"), g("announce"), g("persona_id"), g("retry_of") or item["queue_id"],
                 g("title_override"), g("description_override"), item["queue_id"], item["story_name"],
                 item["chapter_index"], item["platform"], g("content_type") or "story"))
            conn.commit()
        finally:
            conn.close()
    except Exception as e:  # noqa: BLE001 — bookkeeping never breaks the queue
        logger.debug("hand-off adopt skipped (%s)", type(e).__name__)


async def _maybe_slot_announce(item) -> None:
    """Announce a plainly scheduled piece ONCE, after the last row of its time slot (4.43.1).

    The schedule dialog queues one row per site at the same time; each row used to
    announce on its own. Now the piece is announced when no row of it at that time — nor
    any retry of one (``retry_of``) — is still pending / processing and at least one
    posted, following the announce-on-publish switch (`force=None`), exactly as a single
    publish would. Artwork batches have their own rule. A story drip's chapters sit at
    different times, so each release is its own slot.
    """
    try:
        keys = item.keys()
        ctype = item["content_type"] if "content_type" in keys else "story"
        if ctype == "artwork":
            if (item["drip_group"] if "drip_group" in keys else None) or                     (item["announce"] if "announce" in keys else None) is not None:
                return
            where = "q.drip_group IS NULL AND q.announce IS NULL AND "
        elif ctype == "story" and item["action"] == "post":
            where = "q.action = 'post' AND "
        else:
            return
        where = f"q.content_type = '{ctype}' AND q.story_name = ? AND " + where
        root = (item["retry_of"] if "retry_of" in keys else None) or item["queue_id"]
        conn = get_connection()
        try:
            r = conn.execute("SELECT scheduled_at FROM posting_queue WHERE queue_id = ?", (root,)).fetchone()
            slot = r[0] if r else None
            args = [item["story_name"], slot or root]
            origin = (f"SELECT q.queue_id FROM posting_queue q WHERE q.retry_of IS NULL AND {where}"
                      + ("q.scheduled_at = ?" if slot else "q.queue_id = ?"))
            member = f"COALESCE(m.retry_of, m.queue_id) IN ({origin})"
            left = conn.execute(f"SELECT COUNT(*) FROM posting_queue m WHERE {member} "
                                "AND m.status IN ('pending', 'processing')", args).fetchone()[0]
            done = conn.execute(
                f"SELECT m.chapter_index, p.platform, p.external_url FROM posting_queue m "
                f"LEFT JOIN publications p ON p.pub_id = m.pub_id WHERE {member} "
                "AND m.status = 'completed'", args).fetchall()
        finally:
            conn.close()
        if left or not done:
            return
        if ctype == "artwork":
            await manager.announce_artwork(item["story_name"], force=None)
        else:
            await manager.announce_story(
                item["story_name"], first_chapter=min((r[0] or 0) for r in done),
                site_links=[(r[1], r[2]) for r in done if r[1]], force=None)
    except Exception as e:  # noqa: BLE001 — an announcement never breaks the queue
        logger.debug("slot announce skipped (%s)", type(e).__name__)


async def _maybe_batch_announce(item) -> None:
    """Announce a batch piece ONCE, when the last of its rows settles (spec 010).

    A batch queues one row per site; announcing per row would post the same piece to
    Discord once per site. Only rows marked `announce = 1` count, and only when no row
    of the same batch and piece is still pending or processing — so it fires on the last
    one, whatever order they ran in. The links are every site the piece is live on.
    """
    try:
        keys = item.keys()
        if (item["content_type"] if "content_type" in keys else "story") != "artwork":
            return
        if (item["announce"] if "announce" in keys else None) != 1:
            return
        group = item["drip_group"] if "drip_group" in keys else None
        if not group:
            return
        conn = get_connection()
        try:
            left = conn.execute(
                "SELECT COUNT(*) FROM posting_queue WHERE drip_group = ? AND story_name = ? "
                "AND status IN ('pending', 'processing')", (group, item["story_name"])).fetchone()[0]
            # Nothing went out (every row failed) → nothing to announce (4.43.0 review).
            posted = conn.execute(
                "SELECT COUNT(*) FROM posting_queue WHERE drip_group = ? AND story_name = ? "
                "AND status = 'completed'", (group, item["story_name"])).fetchone()[0]
        finally:
            conn.close()
        if left or not posted:
            return
        await manager.announce_artwork(item["story_name"], force=True)
    except Exception as e:  # noqa: BLE001 — an announcement never breaks the queue
        logger.debug("batch announce skipped (%s)", type(e).__name__)


async def _notify_completion(
    story_name: str, chapter_index: int, platform: str,
    action: str, success: bool, error: str | None = None,
    content_type: str = "story",
) -> None:
    """Send a Telegram notification about queue item completion."""
    try:
        settings = config.get_settings()
        if not settings.get("telegram_enabled"):
            return

        from polling.telegram import send_telegram
        emoji = manager.PLATFORM_EMOJIS.get(platform, "📦")
        if content_type == "post":
            # story_name is already a readable snippet; a post has no chapter.
            label = story_name
        else:
            ch_label = f"Ch{chapter_index}" if chapter_index > 0 else "Full"
            label = f"{story_name.replace('_', ' ')} {ch_label}"

        if success:
            text = f"✅ {action.title()} complete: {emoji} {platform.upper()} {label}"
        else:
            text = f"❌ {action.title()} failed: {emoji} {platform.upper()} {label}\n{error or ''}"

        await send_telegram(text)
    except Exception:
        pass  # Don't let notification failures break the scheduler
