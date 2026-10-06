"""Paired comments (spec 021) — templates, per-site defaults, preview, retry.

Routes:
- ``GET/PUT /api/comments/templates`` — saved templates + each site's default
- ``POST /api/comments/preview``      — a piece publish's comments, filled per site
- ``GET /api/comments``                — one owner's comment rows
- ``POST /api/comments/{id}/retry``    — send ONLY the comment, under the live post
- ``DELETE /api/comments/{id}``        — drop an unsent comment
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Query

import config
from database.db import get_connection
from database import comment_queries
from posting import paired_comment

logger = logging.getLogger(__name__)

comments_router = APIRouter(prefix="/api/comments")


@comments_router.get("/templates")
def get_templates():
    tpls, defaults = paired_comment.templates()
    return {"templates": tpls, "defaults": defaults,
            "reply_platforms": list(paired_comment.REPLY_PLATFORMS),
            "placeholders": list(paired_comment.PLACEHOLDERS)}


@comments_router.put("/templates")
def put_templates(payload: dict):
    """Save the whole list + defaults. A deleted template's default goes with it."""
    tpls = payload.get("templates") if isinstance(payload, dict) else None
    defaults = (payload.get("defaults") if isinstance(payload, dict) else None) or {}
    names = {str(t.get("name") or "").strip() for t in (tpls or []) if isinstance(t, dict)}
    if isinstance(defaults, dict):
        defaults = {k: v for k, v in defaults.items() if v in names}
    try:
        clean, clean_defaults = paired_comment.validate_templates(tpls, defaults)
    except ValueError as e:
        raise HTTPException(400, str(e))
    config.save_settings({"comment_templates": clean, "comment_defaults": clean_defaults})
    return {"templates": clean, "defaults": clean_defaults}


@comments_router.post("/preview")
def preview_piece_comments(payload: dict):
    """How a piece publish's comments come out per site. Nothing stored or sent.
    Body: ``{owner: {kind, ref, chapter?}, platforms: [...], comments: {site: text}}``."""
    from posting.post_publisher import comment_preview
    owner = payload.get("owner") if isinstance(payload.get("owner"), dict) else {}
    kind, ref = owner.get("kind"), str(owner.get("ref") or "")
    if kind not in ("artwork", "story") or not ref:
        raise HTTPException(400, "owner must be {kind: artwork|story, ref}")
    comments = payload.get("comments") if isinstance(payload.get("comments"), dict) else {}
    try:
        chapter = int(owner.get("chapter") or 0)
    except (TypeError, ValueError):
        chapter = 0
    sites = {}
    for p in [str(x) for x in (payload.get("platforms") or [])][:24]:
        c = comments.get(p)
        if not c:
            continue
        sites[p] = comment_preview(p, c, [], paired_comment.piece_context(kind, ref, p, chapter),
                                   None, piece=True)
    return {"sites": sites}


@comments_router.get("")
def list_comments(owner_kind: str = Query(...), owner_ref: str = Query(..., max_length=300)):
    if owner_kind not in ("post", "artwork", "story"):
        raise HTTPException(400, "owner_kind must be post, artwork or story")
    conn = get_connection()
    try:
        rows = comment_queries.rows_for_owner(conn, owner_kind, owner_ref)
    finally:
        conn.close()
    return {"comments": [{**paired_comment.summary(r), "platform": r["platform"],
                          "chapter_index": r["chapter_index"]} for r in rows]}


@comments_router.post("/{comment_id}/retry")
async def retry_comment(comment_id: int, payload: dict | None = None):
    """Send only the comment, under the post already live (FR-007). 409 when that post
    isn't live on the site yet — retry the post instead, and its comment follows."""
    payload = payload or {}
    conn = get_connection()
    try:
        row = comment_queries.get(conn, comment_id)
    finally:
        conn.close()
    if not row:
        raise HTTPException(404, "Comment not found")
    if payload.get("background"):
        from posting import activity

        async def _run():
            return await paired_comment.resend(comment_id)
        key = f"{row['platform']}:comment"
        jid = activity.launch("comment", f"{paired_comment.label(row['platform'])} comment", [key],
                              _run, ref={"comment": comment_id})
        return {"status": "started", "job_id": jid}
    try:
        return await paired_comment.resend(comment_id)
    except LookupError:
        raise HTTPException(404, "Comment not found")
    except RuntimeError as e:
        raise HTTPException(409, str(e))


@comments_router.delete("/{comment_id}")
def delete_comment(comment_id: int):
    conn = get_connection()
    try:
        row = comment_queries.get(conn, comment_id)
        if not row:
            raise HTTPException(404, "Comment not found")
        if not comment_queries.delete_unsent(conn, comment_id):
            raise HTTPException(409, "That comment is already live — remove it on the site itself")
    finally:
        conn.close()
    return {"status": "deleted"}
