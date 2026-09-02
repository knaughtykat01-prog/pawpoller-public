"""Posts module (microblog publishing) REST API — 2.49.0.

Compose short-form posts and publish them to microblog platforms (Bluesky +
Mastodon in Phase 2). Mirrors the shape of the Artwork hub API: a library list,
create (multipart so an optional image can ride along), publish, delete, and a
query-param image server.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path

import json

from fastapi import APIRouter, HTTPException, Query, UploadFile, File, Form
from fastapi.responses import FileResponse

import config
from database.db import get_connection
from database import posts_queries
from posting import post_publisher

logger = logging.getLogger(__name__)
posts_router = APIRouter(prefix="/api/posts")

_ALLOWED_EXT = {".png", ".jpg", ".jpeg", ".gif", ".webp"}
_ALLOWED_RATINGS = {"general", "mature", "adult"}
_MAX_IMAGE_BYTES = 25 * 1024 * 1024
_MAX_IMAGES = 4        # X / Bluesky / Mastodon all cap a post at 4 images


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _to_utc_sql(scheduled_at: str) -> str:
    """Parse an ISO 8601 string → UTC 'YYYY-MM-DD HH:MM:SS' (SQLite shape).

    Naive datetimes are treated as UTC; a time >30s in the past is rejected
    (30s grace for clock skew). Same contract as the story/artwork schedulers,
    so a post scheduled for 8pm local fires at 8pm local. Raises HTTPException.
    """
    try:
        dt = datetime.fromisoformat(scheduled_at)
    except ValueError:
        raise HTTPException(400, detail="Invalid datetime format — use ISO 8601")
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    if (dt - datetime.now(timezone.utc)).total_seconds() < -30:
        raise HTTPException(400, detail="Scheduled time must be in the future")
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _media_dir() -> Path:
    d = config.DATA_DIR / "posts_media"
    d.mkdir(parents=True, exist_ok=True)
    return d


@posts_router.get("")
def list_posts(limit: int = Query(100, ge=1, le=500)):
    """The Posts feed — every composed post, newest-first, with publications."""
    conn = get_connection()
    try:
        return {"posts": posts_queries.list_posts(conn, limit=limit)}
    finally:
        conn.close()


@posts_router.get("/image")
def get_post_image(post_id: int = Query(...), idx: int = Query(0, ge=0)):
    """Serve one of a post's attached images (traversal-safe: path derives from
    the post's own stored media, never from user input). `idx` selects which
    image (0-based); it defaults to 0 so existing single-image links still work."""
    conn = get_connection()
    try:
        post = posts_queries.get_post(conn, post_id)
    finally:
        conn.close()
    media = (post or {}).get("media") or []
    if not post or idx >= len(media) or not media[idx].get("path"):
        raise HTTPException(404, "No image for this post")
    p = Path(media[idx]["path"]).resolve()
    if _media_dir().resolve() not in p.parents or not p.is_file():
        raise HTTPException(404, "Image not found")
    return FileResponse(str(p))


# ── Handle-book (contacts) ─────────────────────────────────────────
# Defined BEFORE the "/{post_id}" routes so "/contacts" isn't captured as a
# post id. A contact carries a person's per-platform @handle so the composer can
# tag them with one alias and the publisher expands it per network.

_CONTACT_KEYS = ("name", "handle_bsky", "handle_tw", "handle_mast", "handle_thr", "handle_tum")


@posts_router.get("/contacts")
def list_contacts():
    conn = get_connection()
    try:
        return {"contacts": posts_queries.list_contacts(conn)}
    finally:
        conn.close()


@posts_router.post("/contacts")
def create_contact(payload: dict):
    fields = {k: str(payload.get(k, "") or "") for k in _CONTACT_KEYS}
    if not fields["name"].strip():
        raise HTTPException(400, "A contact needs a name")
    conn = get_connection()
    try:
        cid = posts_queries.add_contact(conn, **fields)
        return {"contact": posts_queries.get_contact(conn, cid)}
    finally:
        conn.close()


@posts_router.patch("/contacts/{contact_id}")
def update_contact(contact_id: int, payload: dict):
    fields = {k: str(payload[k]) for k in _CONTACT_KEYS if k in payload}
    conn = get_connection()
    try:
        if not posts_queries.get_contact(conn, contact_id):
            raise HTTPException(404, "Contact not found")
        posts_queries.update_contact(conn, contact_id, **fields)
        return {"contact": posts_queries.get_contact(conn, contact_id)}
    finally:
        conn.close()


@posts_router.delete("/contacts/{contact_id}")
def delete_contact(contact_id: int):
    conn = get_connection()
    try:
        posts_queries.delete_contact(conn, contact_id)
        return {"status": "deleted"}
    finally:
        conn.close()


# ── Import discovered microblog posts (2.157.0) ───────────────────────────────
# Declared BEFORE the generic `/{post_id}` routes so their literal path segments
# aren't shadowed — same ordering rule the artwork + masterpieces routers follow.
#
# The discovered queue was mostly text tweets, and its only import made an
# *artwork* (downloads an image, mints an artwork folder) — meaningless for a
# post with no image. See posting/post_importer.py for the reasoning.

@posts_router.post("/import/discovered")
def import_all_discovered_posts():
    """Import every discovered text post across the microblog platforms.

    One-click "bring my polled posts in". Per-item failures are collected, not
    fatal. Imported items leave the discovered queue (their `post_publications`
    row is one of its exclusion sets).
    """
    from posting import post_importer
    try:
        return post_importer.import_all_discovered_posts()
    except Exception as e:
        logger.error("Bulk post import failed: %s", e, exc_info=True)
        raise HTTPException(500, detail=str(e))


@posts_router.post("/import/{platform}/{submission_id}")
def import_discovered_post(platform: str, submission_id: str):
    """Import ONE discovered microblog submission as a local post.

    Idempotent — re-importing returns the existing post rather than duplicating.
    """
    from posting import post_importer
    try:
        return post_importer.import_post(platform, submission_id)
    except ValueError as e:
        raise HTTPException(400, detail=str(e))
    except Exception as e:
        logger.error("Post import failed for %s/%s: %s", platform, submission_id, e,
                     exc_info=True)
        raise HTTPException(500, detail=str(e))


@posts_router.get("/{post_id}")
def get_post(post_id: int):
    conn = get_connection()
    try:
        post = posts_queries.get_post(conn, post_id)
        if not post:
            raise HTTPException(404, "Post not found")
        post["publications"] = posts_queries.get_post_publications(conn, post_id)
        return post
    finally:
        conn.close()


@posts_router.post("")
async def create_post(
    body: str = Form(""),
    rating: str = Form("general"),
    image_alt: str = Form(""),
    mentions: str = Form(""),   # JSON [{token, contact_id}] — @alias → contact bindings
    parts: str = Form(""),      # JSON [string] — thread parts 2+ (text-only, gap-wave-3 §4)
    files: list[UploadFile] | None = File(None),
    file: UploadFile | None = File(None),   # legacy single-image field, still accepted
):
    """Create a draft post with up to 4 attached images.

    Accepts the `files` multi-field (current frontend) or a single legacy `file`.
    The first image is also mirrored into the legacy image_path/image_alt columns
    so the feed thumbnail and /image?post_id= (idx 0) keep working unchanged.
    `mentions` binds @alias tokens in the body to handle-book contacts so the
    publisher can expand each alias into the right per-platform handle."""
    body = (body or "").strip()
    rating = rating if rating in _ALLOWED_RATINGS else "general"
    uploads = [f for f in ((files or []) + ([file] if file else [])) if f is not None]
    uploads = uploads[:_MAX_IMAGES]
    if not body and not uploads:
        raise HTTPException(400, "A post needs text or an image")

    conn = get_connection()
    try:
        post_id = posts_queries.create_post(
            conn, body=body, rating=rating, image_alt=image_alt, now=_now())
        # Thread parts (gap-wave-3 §4): each part is a child post row, text-only.
        if parts:
            try:
                part_texts = json.loads(parts)
            except (ValueError, TypeError):
                part_texts = []
            if isinstance(part_texts, list):
                for i, txt in enumerate([t for t in part_texts
                                         if isinstance(t, str) and t.strip()][:24]):
                    posts_queries.create_post(
                        conn, body=txt.strip(), rating=rating, now=_now(),
                        parent_post_id=post_id, thread_ordinal=i + 1)
        bindings = []
        if mentions:
            try:
                parsed = json.loads(mentions)
                if isinstance(parsed, list):
                    bindings = parsed
            except (ValueError, TypeError):
                bindings = []   # malformed → just skip tagging, don't fail the post
        if bindings:
            posts_queries.set_post_mentions(conn, post_id, bindings)
    finally:
        conn.close()

    first_path = ""
    for idx, up in enumerate(uploads):
        ext = Path(up.filename or "").suffix.lower()
        if ext not in _ALLOWED_EXT:
            _cleanup(post_id)
            raise HTTPException(400, "Images must be PNG, JPG, GIF or WebP")
        data = await up.read()
        if len(data) > _MAX_IMAGE_BYTES:
            _cleanup(post_id)
            raise HTTPException(400, "An image is too large (max 25 MB)")
        dest = _media_dir() / f"{post_id}_{idx}{ext}"
        dest.write_bytes(data)
        conn = get_connection()
        try:
            posts_queries.add_post_media(
                conn, post_id=post_id, ordinal=idx, path=str(dest),
                alt=image_alt if idx == 0 else "")
        finally:
            conn.close()
        if idx == 0:
            first_path = str(dest)

    if first_path:
        conn = get_connection()
        try:
            posts_queries.update_post(conn, post_id, image_path=first_path,
                                      image_alt=image_alt, now=_now())
        finally:
            conn.close()

    return {"post_id": post_id}


@posts_router.post("/{post_id}/publish")
async def publish_post(post_id: int, payload: dict):
    """Publish a composed post to the chosen platforms."""
    platforms = payload.get("platforms") or []
    account_ids = payload.get("account_ids") or {}
    if not platforms:
        raise HTTPException(400, "Pick at least one platform")
    try:
        results = await post_publisher.publish_post(post_id, platforms, account_ids)
    except ValueError as e:
        raise HTTPException(404, str(e))
    except Exception as e:
        logger.error("publish_post failed: %s", e, exc_info=True)
        raise HTTPException(500, str(e))
    successes = sum(1 for r in results if r.get("success"))
    return {"results": results, "successes": successes, "failures": len(results) - successes}


@posts_router.post("/{post_id}/schedule")
async def schedule_post(post_id: int, payload: dict):
    """Schedule a composed post to publish to the chosen platforms at a future time.

    Body: { "platforms": ["bsky","mast"], "account_ids": {"bsky": 3},
            "scheduled_at": "2026-07-25T20:00:00.000Z" }

    One `posting_queue` row per platform (`content_type='post'`, `story_name` =
    the post_id, a body snippet stashed as `title_override` so the Queue &
    Schedule page shows something readable). The posting-scheduler daemon fires
    each row when due via `post_publisher.publish_post`.
    """
    from posting.manager import get_platform_requires
    from database import posting_queries

    platforms = payload.get("platforms") or []
    account_ids = payload.get("account_ids") or {}
    scheduled_at = payload.get("scheduled_at")
    if not platforms:
        raise HTTPException(400, "Pick at least one platform")
    if not scheduled_at:
        raise HTTPException(400, "scheduled_at is required")

    conn = get_connection()
    try:
        post = posts_queries.get_post(conn, post_id)
    finally:
        conn.close()
    if not post:
        raise HTTPException(404, "Post not found")

    scheduled_str = _to_utc_sql(scheduled_at)
    snippet = " ".join((post.get("body") or "").split())[:60] or f"Post #{post_id}"

    queue_ids = []
    conn = get_connection()
    try:
        for platform in platforms:
            qid = posting_queries.add_to_queue(
                conn, str(post_id), 0, platform, action="post",
                account_id=account_ids.get(platform),
                content_type="post",
                scheduled_at=scheduled_str,
                title_override=snippet,
                requires=get_platform_requires(platform),
            )
            queue_ids.append(qid)
    finally:
        conn.close()

    logger.info("Scheduled post #%d to %s at %s (queue %s)",
                post_id, ",".join(platforms), scheduled_str, queue_ids)
    return {"ok": True, "queue_ids": queue_ids, "scheduled_at": scheduled_str}


@posts_router.delete("/{post_id}")
def delete_post(post_id: int):
    conn = get_connection()
    try:
        post = posts_queries.get_post(conn, post_id)
        if not post:
            raise HTTPException(404, "Post not found")
        posts_queries.delete_post(conn, post_id)
    finally:
        conn.close()
    # Best-effort media cleanup — every attached image, de-duplicated (the
    # legacy image_path mirrors media[0]).
    paths = {m.get("path") for m in (post.get("media") or []) if m.get("path")}
    if post.get("image_path"):
        paths.add(post["image_path"])
    for img in paths:
        try:
            p = Path(img).resolve()
            if _media_dir().resolve() in p.parents and p.is_file():
                p.unlink()
        except OSError:
            pass
    return {"status": "deleted"}


def _cleanup(post_id: int) -> None:
    """Delete a just-created post row after an image-validation failure."""
    conn = get_connection()
    try:
        posts_queries.delete_post(conn, post_id)
    finally:
        conn.close()
