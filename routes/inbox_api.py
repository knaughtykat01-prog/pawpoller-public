"""Unified comment inbox API (gap G3).

- ``GET  /api/inbox``          — the cross-platform comment feed (IB + FA legacy
  tables ∪ platform_comments), newest-first, with handled flags.
- ``POST /api/inbox/handled``  — mark/unmark one comment handled.
- ``POST /api/inbox/reply``    — native reply where the platform supports it
  (Stage B: bsky / mast / e621). Everything else replies on-site via permalink.
- ``POST /api/inbox/backfill-own`` — re-run own-comment detection (2.192.0).

Reply creds resolve exactly like the Posts publisher (explicit account, else the
platform default, else legacy flat keys) — the comment row remembers which of
our accounts owns the submission it sits under.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException

from database.db import get_connection
from database import inbox_queries
from polling import self_comment

logger = logging.getLogger(__name__)
inbox_router = APIRouter(prefix="/api/inbox", tags=["inbox"])

_REPLYABLE = {"bsky", "mast", "e621"}


_own_backfilled = False


@inbox_router.get("")
def get_inbox(platform: str | None = None, unhandled: bool = False,
              limit: int = 200):
    """The unified inbox + an unhandled count for the nav badge.

    Own comments are reported handled by get_inbox itself (2.192.0), so they can
    never reach the badge count.
    """
    global _own_backfilled
    conn = get_connection()
    try:
        # Retro-flag historical self-comments once per process — opening the
        # Inbox is the first place a user would notice them. Not run from
        # _run_migrations because Mastodon/Bluesky handles are only known after
        # a login has happened.
        if not _own_backfilled:
            _own_backfilled = True
            try:
                self_comment.backfill_own_comments(conn)
            except Exception as e:  # noqa: BLE001 — never fail the feed over it
                logger.warning("Self-comment backfill skipped: %s", e)
        items = inbox_queries.get_inbox(conn, platform=platform or None,
                                        unhandled_only=unhandled,
                                        limit=max(1, min(limit, 500)))
        unhandled_count = sum(
            1 for i in inbox_queries.get_inbox(conn, limit=500) if not i["handled"])
    finally:
        conn.close()
    return {"items": items, "unhandled_count": unhandled_count}


@inbox_router.post("/backfill-own")
def backfill_own():
    """Re-run own-comment detection over stored comments.

    Worth calling after connecting an account or fixing a handle: identity is
    resolved at login, so rows captured before that was known stay unflagged
    until this runs.
    """
    conn = get_connection()
    try:
        flagged = self_comment.backfill_own_comments(conn)
    finally:
        conn.close()
    return {"ok": True, "flagged": flagged,
            "total": sum(flagged.values()) if flagged else 0}


@inbox_router.post("/handled")
def set_handled(body: dict):
    platform = (body or {}).get("platform") or ""
    comment_id = str((body or {}).get("comment_id") or "")
    if not (platform and comment_id):
        raise HTTPException(400, "platform and comment_id are required")
    conn = get_connection()
    try:
        inbox_queries.set_handled(conn, platform, comment_id,
                                  bool(body.get("handled", True)))
    finally:
        conn.close()
    return {"ok": True}


def _get_platform_comment(platform: str, comment_id: str) -> dict | None:
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT * FROM platform_comments WHERE platform = ? AND comment_id = ?",
            (platform, str(comment_id)),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


@inbox_router.post("/reply")
async def reply(body: dict):
    """Native reply (Stage B). Body: {platform, comment_id, text}.

    On success the comment is auto-marked handled. Platform-specific notes:
    Mastodon needs a write-scope token (a poll-only read token 403s — surfaced
    plainly); Bluesky threads the reply with the stored uri/cid refs; e621
    comments land flat on the post (the site has no per-comment threading).
    """
    import json as _json

    platform = (body or {}).get("platform") or ""
    comment_id = str((body or {}).get("comment_id") or "")
    text = ((body or {}).get("text") or "").strip()
    if not (platform and comment_id and text):
        raise HTTPException(400, "platform, comment_id and text are required")
    if platform not in _REPLYABLE:
        raise HTTPException(400, f"Native reply isn't supported on {platform} — "
                                 "use the comment's permalink to reply on-site.")

    row = _get_platform_comment(platform, comment_id)
    if not row:
        raise HTTPException(404, "Comment not found")
    meta = {}
    try:
        meta = _json.loads(row.get("meta") or "{}")
    except (TypeError, ValueError):
        pass

    from posting.post_publisher import _resolve_creds
    account_id, creds = _resolve_creds(platform, row.get("account_id"), None)

    ok, url = False, ""
    if platform == "bsky":
        ident = creds.get("bsky_identifier", "")
        pw = creds.get("bsky_app_password", "")
        if not (ident and pw):
            raise HTTPException(400, "Bluesky account isn't connected")
        if not (meta.get("cid") and meta.get("root_uri") and meta.get("root_cid")):
            raise HTTPException(400, "Missing thread refs for this comment — "
                                     "re-poll Bluesky and try again.")
        from clients.bsky.client import BskyClient
        client = BskyClient(identifier=ident, app_password=pw)
        try:
            r = await client.create_post(text, reply={
                "root": {"uri": meta["root_uri"], "cid": meta["root_cid"]},
                "parent": {"uri": comment_id, "cid": meta["cid"]},
            })
        finally:
            await client.close()
        ok = bool(r and r.get("uri"))
        url = (r or {}).get("url", "")

    elif platform == "mast":
        instance = creds.get("mast_instance_url", "")
        token = creds.get("mast_access_token", "")
        if not (instance and token):
            raise HTTPException(400, "Mastodon account isn't connected")
        from clients.mast.client import MastClient
        client = MastClient(instance_url=instance, access_token=token)
        try:
            r = await client.create_status(text, in_reply_to_id=comment_id)
        finally:
            await client.close()
        if r is None:
            raise HTTPException(502, "Mastodon rejected the reply — a poll-only "
                                     "token can't post (needs a write scope).")
        ok = True
        url = r.get("url", "")

    elif platform == "e621":
        user = creds.get("e621_username", "")
        key = creds.get("e621_api_key", "")
        if not (user and key):
            raise HTTPException(400, "e621 account isn't connected")
        from clients.e621.client import E621Client
        client = E621Client(username=user, api_key=key)
        try:
            ok = await client.post_comment(row["submission_id"], text)
        finally:
            await client.close()
        url = row.get("permalink", "")

    if not ok:
        raise HTTPException(502, f"{platform} rejected the reply — check the logs.")

    conn = get_connection()
    try:
        inbox_queries.set_handled(conn, platform, comment_id, True)
    finally:
        conn.close()
    logger.info("Inbox reply posted on %s (comment %s, account %s)",
                platform, comment_id[:60], account_id)
    return {"ok": True, "url": url}
