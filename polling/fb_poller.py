"""Facebook Pages (fb) poll cycle — spec 029 (4.59.0).

Per account (one Page, its Page token):
  1. the Page's posts, newest first (clients/fb/client.py list_posts) → fb_submissions;
  2. their numbers in batched calls (post_stats) → snapshot whenever one moved;
  3. Page followers → the shared follower series;
  4. comments on posts whose count went up → the Inbox (inbox_capture).

Views / reactions-by-type / plays are Page insights and need ``read_insights``; comments need
``pages_read_user_content``. A Page connected before 4.59.0 lacks both: the poll still stores what it
can, ends ``partial`` and names the permission (the dashboard shows it). Posting is never touched.
Network calls run with no write transaction open (poller gotcha: no write lock across an await).
"""
from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timezone

import config
from clients.fb.client import FbClient, FbPermissionError
from database.db import get_connection
from database import fb_queries
from polling.followers import capture_followers
from polling.notifications import describe_error

logger = logging.getLogger(__name__)

fb_poll_progress = {"active": False, "phase": "idle", "current": 0, "total": 0, "message": ""}

_fb_poll_lock = threading.Lock()

# ponytail: a normal poll reads the newest 200 posts; Full resync reads them all (and only a pass that
# reached the end flags vanished posts deleted). Raise if Pages with > 200 live-moving posts appear.
_QUICK_PAGES = 4


def _progress(phase: str, message: str = "", current: int = 0, total: int = 0) -> None:
    fb_poll_progress.update(active=phase not in ("idle", "complete", "error"),
                            phase=phase, current=current, total=total, message=message)


async def run_fb_poll_cycle(account_id: int | None = None, force_full: bool = False,
                            client: FbClient | None = None) -> dict:
    """One Facebook poll for one account. ``client`` is for tests; a real cycle builds its own."""
    from database import accounts as accounts_db
    _ac = get_connection()
    try:
        if account_id is None:
            account_id = accounts_db.get_default_account_id(_ac, "fb", create=True)
        row = accounts_db.get_account(_ac, account_id)
    finally:
        _ac.close()
    is_default = bool(row["is_default"]) if row else True

    if not _fb_poll_lock.acquire(blocking=False):
        logger.warning("fb poll already running -- skipping (account %s)", account_id)
        return {}
    _progress("starting", "Reading your Facebook Page...")

    conn = None
    log_id = None
    start = time.time()
    stats = {"submissions_found": 0, "snapshots_inserted": 0, "new_comments": 0}
    missing = ""
    creds = config.resolve_account_credentials("fb", account_id, is_default, config.get_settings())
    own = client is None
    client = client or FbClient(creds.get("fb_page_token", ""), creds.get("fb_page_id", ""))
    try:
        conn = get_connection()
        log_id = fb_queries.start_poll_log(conn, account_id)
        conn.close()
        conn = None

        if not client.page_token or not client.page_id:
            raise ValueError("Facebook isn't connected -- connect your Page in Settings → Platforms → Facebook")

        # 1. posts (network only)
        posts, after, reached_end = [], "", False
        for _ in range(10_000 if force_full else _QUICK_PAGES):
            _progress("searching", f"Reading posts... {len(posts)} so far")
            page, after = await client.list_posts(limit=50, after=after)
            posts.extend(page)
            if not after:
                reached_end = True
                break

        # 2. numbers (network only)
        _progress("stats", f"Reading the numbers for {len(posts)} posts...", 0, len(posts))
        numbers, missing = await client.post_stats(
            [{"id": p["id"], "video": "video" in p["media_type"].lower()} for p in posts])

        poll_ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        conn = get_connection()
        for p in posts:
            fb_queries.upsert_post(conn, p, account_id, client.page_id)
            if fb_queries.save_numbers(conn, p["id"], numbers.get(p["id"]) or {}, account_id, poll_ts):
                stats["snapshots_inserted"] += 1
        stats["submissions_found"] = len(posts)
        if reached_end:
            fb_queries.mark_gone(conn, account_id, {p["id"] for p in posts})
        fb_queries.link_publications(conn)
        conn.commit()

        # 3. followers (commits its own write)
        await capture_followers(client, account_id, conn)

        # 4. comments → Inbox
        _progress("comments", "Checking for new comments...")
        comment_refused = []

        async def _fetch(c):
            try:
                rows = await client.get_comments(c["submission_id"])
            except FbPermissionError as e:
                comment_refused.append(e.permission)
                return []
            return [{**r, "meta": {"post_id": c["submission_id"], "parent_id": r["parent_id"],
                                   "author_id": r["author_id"]}} for r in rows]

        try:
            from polling.inbox_capture import capture as _inbox_capture
            stats["new_comments"] = await _inbox_capture(
                conn, "fb",
                [{"submission_id": p["id"], "fresh_count": (numbers.get(p["id"]) or {}).get("comments") or 0,
                  "title": (p["message"].strip().splitlines() or ["Facebook post"])[0][:120]} for p in posts],
                _fetch, account_id=account_id, own_author=creds.get("fb_page_name", ""))
        except Exception as ce:  # noqa: BLE001 — capture never fails the poll
            logger.warning("fb inbox capture failed: %s", ce)
        missing = missing or (comment_refused[0] if comment_refused else "")

        duration = time.time() - start
        status = "partial" if missing else "success"
        _progress("complete", f"Done -- {len(posts)} posts, {stats['snapshots_inserted']} changed"
                              + (f" (add {missing} to see everything)" if missing else ""))
        fb_queries.finish_poll_log(conn, log_id, status, missing_permission=missing,
                                   duration_seconds=duration, **stats)
        logger.info("fb poll %s in %.1fs -- %d posts, %d snapshots, %d new comments%s", status, duration,
                    len(posts), stats["snapshots_inserted"], stats["new_comments"],
                    f", missing {missing}" if missing else "")
        return stats
    except Exception as e:
        duration = time.time() - start
        _progress("error", describe_error(e))
        logger.error("fb poll failed: %s", describe_error(e), exc_info=True)
        if log_id:
            c = conn or get_connection()
            try:
                fb_queries.finish_poll_log(c, log_id, "error", error_message=describe_error(e),
                                           missing_permission=missing, duration_seconds=duration, **stats)
            finally:
                if c is not conn:
                    c.close()
        from polling.telegram import send_poll_error
        try:
            await send_poll_error("fb", e)
        except Exception:
            logger.debug("Error alert send failed", exc_info=True)
        raise
    finally:
        _fb_poll_lock.release()
        if conn:
            conn.close()
        if own:
            await client.close()
