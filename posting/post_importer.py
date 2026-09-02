"""Import a discovered microblog post into the Posts module (2.157.0).

Reported: *"looking at the discovered. its all tweets and stuff without images. So for
those, they should be imported into posts no?"* — and the numbers backed it up: of
62 discovered items, 60 carried no image and 54 were tweets. The only import the
discovered queue offered was **import-as-artwork**, which downloads an image and
mints an artwork folder. For a text tweet there is no image to download, so the
whole queue was a wall of items with no sensible action but Ignore.

A tweet is a **post**, and PawPoller already has a Posts module with exactly the
right shape (`posts.body` + a `post_publications` row per platform). So this
imports the poller's own row into it — the text-side mirror of
`artwork_importer`.

Scope — deliberately text-only:
  Image-bearing items already have a good home (Import → artwork, ★ Master →
  Masterpiece). Importing those here would mean either downloading media into
  `posts_media/` or silently dropping the image. So the button only appears on
  microblog items with **no image**: image → artwork, text → post, no overlap and
  nothing lost either way.

The account matters. Each poller row carries the `account_id` it was found under,
and a user's posts routinely span several accounts on one platform. Carrying it
through is what keeps an imported post attributed to the right persona instead of
every one landing on the platform default — the same bug that lumped personas
together until 2.96.0.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from database.db import get_connection
from database import posts_queries

logger = logging.getLogger(__name__)

# Platforms whose submissions ARE posts. A SquidgeWorld text work or a
# DeviantArt piece is a story/artwork that happens to lack a thumbnail — it is
# NOT a microblog post, and must never get the "→ Posts" action.
MICROBLOG_PLATFORMS = {"tw", "bsky", "mast", "thr", "tum"}


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def is_importable_post(item: dict) -> bool:
    """Is this discovered item a text post we can import into the Posts module?

    Microblog platform + no image. That's the whole test, and it deliberately does
    NOT consult ``kind``.

    ``kind`` is the wrong signal here: ``classify_kind`` lists ``"post"`` among its
    ART hints (so that an image-bearing Bluesky/Mastodon post is catchable by the
    artwork import), which means **every** Bluesky post is tagged ``art`` no matter
    what it contains. Gating on ``kind != "art"`` therefore hid this button from
    exactly the imageless microblog posts it exists for.

    No image means there is nothing for the artwork path to download — the bulk art
    import filters on ``thumbnail_url`` for the same reason — so on a microblog
    that is a text post, whatever the type string happens to say.
    """
    return bool(item) and item.get("platform") in MICROBLOG_PLATFORMS \
        and not item.get("thumbnail_url")


def already_imported(conn, platform: str, submission_id: str) -> int | None:
    """The post_id this submission was already imported as, if any.

    Import is idempotent: re-importing returns the existing post rather than
    minting a duplicate. `post_publications` is also what makes the item leave
    the discovered queue (see `get_discovered_unlinked`), so this is the same
    check from both directions.
    """
    row = conn.execute(
        "SELECT post_id FROM post_publications WHERE platform = ? AND external_id = ?",
        (platform, str(submission_id)),
    ).fetchone()
    return int(row["post_id"]) if row else None


def import_post(platform: str, submission_id: str) -> dict:
    """Import ONE discovered microblog submission as a local post.

    Reuses the metadata the poller already stored — no network call. Returns
    ``{status: imported|skipped, post_id, platform, submission_id}``.
    """
    from posting.sync import PLATFORM_TABLES

    if platform not in MICROBLOG_PLATFORMS:
        raise ValueError(
            f"{platform} is not a microblog platform — its submissions are "
            f"stories or artwork, not posts.")

    cfg = PLATFORM_TABLES.get(platform)
    if not cfg:
        raise ValueError(f"Unknown platform: {platform}")

    conn = get_connection()
    try:
        existing = already_imported(conn, platform, submission_id)
        if existing:
            return {"status": "skipped", "post_id": existing, "platform": platform,
                    "submission_id": str(submission_id), "reason": "already imported"}

        id_col = cfg.get("id_col", "submission_id")
        row = conn.execute(
            f"SELECT * FROM {cfg['table']} WHERE {id_col} = ?", (str(submission_id),)
        ).fetchone()
        if not row:
            raise ValueError(f"No stored {platform} submission {submission_id}")
        d = dict(row)

        # `description` is the full post text; `title` is usually the same string
        # (often truncated for display). Prefer description, fall back to title.
        body = (d.get("description") or "").strip() or (d.get("title") or "").strip()
        if not body:
            raise ValueError("Submission has no text to import")

        post_id = posts_queries.create_post(
            conn,
            body=body,
            rating=(d.get("rating") or "general"),
            # No media by design — this path is text-only.
            now=(d.get("posted_at") or _now()),
        )
        posts_queries.upsert_post_publication(
            conn,
            post_id=post_id,
            platform=platform,
            # Carry the source account so the post is attributed to the RIGHT
            # persona (else everything lands on the platform default — the 2.96.0
            # "lumped personas" bug).
            account_id=int(d.get("account_id") or 0),
            status="posted",
            external_id=str(submission_id),
            external_url=(d.get("link") or ""),
            now=(d.get("posted_at") or _now()),
        )
    finally:
        conn.close()

    logger.info("Imported %s/%s as post %d", platform, submission_id, post_id)
    return {"status": "imported", "post_id": post_id, "platform": platform,
            "submission_id": str(submission_id)}


def import_all_discovered_posts() -> dict:
    """Import every discovered text post across the microblog platforms.

    One bad item must never abort the batch — failures are collected and
    reported, mirroring the artwork bulk import.
    """
    from routes.submissions_api import get_discovered_unlinked

    conn = get_connection()
    try:
        items = [it for it in get_discovered_unlinked(conn) if is_importable_post(it)]
    finally:
        conn.close()

    imported, skipped, failed = [], [], []
    for it in items:
        plat, sid = it["platform"], it["submission_id"]
        try:
            res = import_post(plat, sid)
            (imported if res["status"] == "imported" else skipped).append(res)
        except Exception as e:
            failed.append({"platform": plat, "submission_id": sid,
                           "title": (it.get("title") or "")[:60], "error": str(e)[:160]})
    return {
        "total": len(items),
        "imported": len(imported),
        "skipped": len(skipped),
        "failed": len(failed),
        "failures": failed[:25],
    }
