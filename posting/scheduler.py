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

    while True:
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


async def _process_queue_item(item: dict) -> None:
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
            )
        elif action == "post":
            results = await manager.post_story(
                story_name, [platform], [chapter_index],
                account_ids={platform: account_id} if account_id else None,
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
        await _notify_completion(notify_name, chapter_index, platform, action, False, str(e),
                                 content_type=content_type)


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
