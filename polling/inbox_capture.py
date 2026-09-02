"""Stage-A1 inbox comment capture (gap G3) — shared by the bsky/mast/e621/da pollers.

The delta check is capture-count based, not snapshot based: fetch a submission's
thread when the platform's fresh comment/reply count exceeds how many rows we've
already captured for it. Self-healing (a missed fetch retries next cycle) and
schema-decoupled. Capped per cycle so a first-run backfill spreads across
cycles instead of hammering a platform.

Poller rule respected: callers invoke this AFTER their post-loop commit, and the
network fetches here run with no write transaction open (each upsert commits
itself immediately).

Our OWN comments are stored too (so captured counts track platform counts and
the fetch doesn't re-trigger forever) but are flagged ``is_own`` and excluded
from the returned new-comment count — the inbox shows them only as context,
never as things to answer. Identity matching lives in ``polling/self_comment``;
see its docstring for the local-part false positive this replaced (2.192.0).
"""
from __future__ import annotations

import logging

from polling import self_comment

logger = logging.getLogger(__name__)

_CAP = 25   # max thread fetches per platform per cycle


async def capture(conn, platform: str, candidates: list[dict], fetch, *,
                  account_id: int | None = None, own_author: str = "") -> int:
    """Capture comment content for submissions that look behind.

    Args:
        candidates: [{submission_id, fresh_count, title, ...}] — one per polled
            submission (fresh_count = the platform's current comment count).
        fetch: async fn(candidate) -> [{comment_id, author, body, commented_at,
            permalink, meta?}] — one platform-specific thread fetch.
        own_author: our account's handle/username as resolved at login. Merged
            with the persisted/settings identity and used to flag our own
            comments ``is_own``. Optional — when empty, the persisted handle is
            used instead of silently disabling the filter.

    Returns the number of NEW comments captured, EXCLUDING our own.
    """
    from database import inbox_queries

    todo = []
    for c in candidates:
        if (c.get("fresh_count") or 0) <= 0 or not c.get("submission_id"):
            continue
        have = inbox_queries.count_for_submission(conn, platform, c["submission_id"])
        if c["fresh_count"] > have:
            todo.append(c)
        if len(todo) >= _CAP:
            break
    if not todo:
        return 0

    # Our own identity, widened to every form that means "us" on this platform
    # (persisted handle + the platform's canonical identity key, plus Mastodon's
    # bare local part). The incoming author is compared WHOLE — the pre-2.192
    # code split the host off both sides, so @sam@some.other.instance matched
    # our own @sam@our.instance and a stranger's comment was marked as ours.
    handles = self_comment.own_handles(conn, platform, account_id)
    if own_author:
        handles = handles | {self_comment.normalise_handle(own_author)}
        # The caller resolved a handle at login; persist it so the read-side
        # filters and the backfill (which have no client instance) can use it.
        self_comment.remember_own_handle(conn, platform, own_author, account_id)

    captured = 0
    for c in todo:
        try:
            comments = await fetch(c)          # network — no write txn open
        except Exception as e:  # noqa: BLE001 — capture must never fail a poll
            logger.warning("%s inbox capture fetch failed for %s: %s",
                           platform, str(c["submission_id"])[:60], e)
            continue
        for r in comments or []:
            if not r.get("comment_id"):
                continue
            mine = self_comment.is_own_author(r.get("author", ""), handles)
            is_new = inbox_queries.upsert_platform_comment(
                conn, platform, r["comment_id"], c["submission_id"],
                author=r.get("author", ""), body=r.get("body", ""),
                commented_at=r.get("commented_at"),
                permalink=r.get("permalink", ""),
                submission_title=c.get("title", ""),
                account_id=account_id, meta=r.get("meta") or {},
                is_own=mine,
            )
            # Own comments are stored (so captured count tracks the platform's
            # count and the thread stops re-fetching) but never counted as new
            # engagement. get_inbox derives handled from is_own, so no
            # inbox_state write is needed here any more.
            if is_new and not mine:
                captured += 1
    if captured:
        logger.info("%s inbox capture: %d new comment(s) across %d submission(s)",
                    platform, captured, len(todo))
    return captured
