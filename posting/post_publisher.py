"""Publish composed microblog posts to their platforms — 2.49.0.

The compose→publish engine for the Posts module. Deliberately lightweight: it
constructs a **fresh** platform client per publish from the account's resolved
credentials (never the poller singletons — posting must not mutate a client
mid-poll), calls that client's create method, and records the outcome in
``post_publications``.

Phase 2 wires Bluesky + Mastodon (both post fine from any IP). Threads, Tumblr
and X are recognised but return a clear "not wired yet" error until Phase 3.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Any

import config
from database.db import get_connection
from database import accounts as accounts_db
from database import posts_queries

logger = logging.getLogger(__name__)

# Platforms this module can post to.
SUPPORTED = ("bsky", "mast", "thr", "tw", "tum", "ig", "tg")

# These still post text only (image cross-posting needs per-platform work:
# Threads wants a public image_url, Tumblr NPF). X gained image posting in 2.58.0.
_TEXT_ONLY = ("thr", "tum")

# The inverse of _TEXT_ONLY: platforms that REQUIRE an image — Instagram has no
# text-only feed post, so a caption alone can't be published.
_IMAGE_REQUIRED = ("ig",)

# Rating → Bluesky self-labels. General adds none.
_BSKY_LABELS = {"mature": ["sexual"], "adult": ["porn"]}
_SENSITIVE_RATINGS = ("mature", "adult")


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


async def _relay_stash_image(server_url: str, api_key: str, path: str) -> str:
    """Upload an image to a paired PawPoller server's IG image host; return the
    public URL Meta will fetch.

    Used from a desktop instance, which has no public address of its own: it
    borrows the server as the image host (the same pairing — ``posting_server_url``
    + ``posting_server_api_key`` — used for story/artwork sync). Raises on any
    failure so the publish surfaces a clear error instead of a broken post.
    """
    import httpx
    from pathlib import Path
    data = Path(path).read_bytes()
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    url = server_url.rstrip("/") + "/api/ig/pubmedia"
    async with httpx.AsyncClient(timeout=90.0) as http:
        resp = await http.post(
            url,
            files={"file": (Path(path).name, data, "application/octet-stream")},
            headers=headers,
        )
    if resp.status_code != 200:
        raise RuntimeError(f"image relay to {url} failed ({resp.status_code}): {resp.text[:200]}")
    public = (resp.json() or {}).get("url")
    if not public:
        raise RuntimeError("image relay returned no URL")
    return public


def _render_body(body: str, mentions: list[dict], platform: str) -> str:
    """Expand each bound @alias in the body into this platform's handle.

    ``mentions`` are the post's bindings (from ``get_post_mentions``), each a
    dict with a ``token`` (the alias, no @) and per-platform ``handle_*`` fields.
    A binding with no handle for this platform (or a deleted contact) is left as
    the plain ``@alias`` text — so nothing is dropped, it just isn't linked there.
    Substitution is whole-token (``@luna`` won't touch ``@lunar``).
    """
    field = "handle_" + platform
    out = body or ""
    for m in (mentions or []):
        token = (m.get("token") or "").strip()
        if not token:
            continue
        handle = (m.get(field) or "").strip().lstrip("@")
        if not handle:
            continue
        out = re.sub(r"@" + re.escape(token) + r"\b", "@" + handle, out)
    return out


def _resolve_creds(platform: str, account_id: int | None,
                   settings: dict | None) -> tuple[int, dict]:
    """Return (account_id, {canonical_field: value}) for the target account.

    Mirrors the pollers: an explicit account_id wins, else the platform's
    default account; falls back to the legacy single-account keys (is_default)
    when no account row exists.
    """
    conn = get_connection()
    try:
        if account_id is None:
            account_id = accounts_db.get_default_account_id(conn, platform, create=False)
        acct = accounts_db.get_account(conn, account_id) if account_id else None
    finally:
        conn.close()
    is_default = bool(acct["is_default"]) if acct else True
    creds = config.resolve_account_credentials(platform, account_id or 0, is_default, settings)
    return (account_id or 0, creds)


async def _publish_one(post: dict, platform: str, account_id: int | None,
                       settings: dict | None) -> dict[str, Any]:
    """Post one composed post to one platform. Returns a result dict; never raises."""
    result: dict[str, Any] = {
        "platform": platform, "account_id": account_id or 0,
        "success": False, "external_id": "", "external_url": "", "error": "",
    }
    if platform not in SUPPORTED:
        result["error"] = f"posting to {platform} isn't wired yet"
        return result

    body = post.get("body", "")
    mentions = post.get("mentions") or []
    text = _render_body(body, mentions, platform)   # @alias → this platform's handle
    rating = (post.get("rating") or "general").lower()
    media = post.get("media") or []
    if not media and post.get("image_path"):        # legacy-shaped post dict
        media = [{"path": post["image_path"], "alt": post.get("image_alt", "")}]
    image_paths = [m["path"] for m in media if m.get("path")][:4]
    image_alts = [m.get("alt", "") for m in media if m.get("path")][:4]

    if platform in _TEXT_ONLY and image_paths:
        result["error"] = (f"{platform} posting is text-only for now — drop the image, "
                           f"or use Bluesky/Mastodon/X for image posts")
        return result

    if platform in _IMAGE_REQUIRED and not image_paths:
        result["error"] = ("Instagram requires a photo — attach an image "
                           "(Instagram has no text-only posts)")
        return result

    account_id, creds = _resolve_creds(platform, account_id, settings)
    result["account_id"] = account_id

    try:
        if platform == "bsky":
            from clients.bsky.client import BskyClient
            ident = creds.get("bsky_identifier", "")
            pw = creds.get("bsky_app_password", "")
            if not (ident and pw):
                result["error"] = "Bluesky account isn't connected"
                return result
            client = BskyClient(identifier=ident, app_password=pw)
            # Bluesky needs explicit rich-text facets to link #tags and @mentions
            # (unlike X/Mastodon, which auto-link server-side). Pass the bound
            # contacts' Bluesky handles so the client resolves each to a DID and
            # builds a mention facet at its position in the rendered text.
            bsky_mentions = [(m.get("handle_bsky") or "").strip()
                             for m in mentions if (m.get("handle_bsky") or "").strip()]
            try:
                r = await client.create_post(
                    text, image_paths=image_paths, image_alts=image_alts,
                    labels=_BSKY_LABELS.get(rating) or None,
                    mention_handles=bsky_mentions or None,
                )
            finally:
                await client.close()
            if r and r.get("uri"):
                result.update(success=True, external_id=r.get("uri", ""),
                              external_url=r.get("url", ""))
                # Thread chaining refs (gap-wave-3 §4) — in-memory only.
                result["_refs"] = {"uri": r.get("uri", ""), "cid": r.get("cid", "")}
            else:
                result["error"] = "Bluesky rejected the post (check the app password / logs)"

        elif platform == "mast":
            from clients.mast.client import MastClient
            instance = creds.get("mast_instance_url", "")
            token = creds.get("mast_access_token", "")
            if not (instance and token):
                result["error"] = "Mastodon account isn't connected"
                return result
            client = MastClient(instance_url=instance, access_token=token)
            try:
                r = await client.create_status(
                    text, image_paths=image_paths, image_alts=image_alts,
                    sensitive=(rating in _SENSITIVE_RATINGS),
                    idempotency_key=f"pp-{post.get('post_id')}-mast",
                )
            finally:
                await client.close()
            if r and (r.get("id") or r.get("uri")):
                result.update(success=True, external_id=r.get("id", "") or r.get("uri", ""),
                              external_url=r.get("url", ""))
            else:
                result["error"] = ("Mastodon rejected the post — the access token likely "
                                    "needs a write scope (check the app / logs)")

        elif platform == "thr":
            from clients.thr.client import ThrClient
            token = creds.get("thr_access_token", "")
            if not token:
                result["error"] = "Threads account isn't connected"
                return result
            client = ThrClient(access_token=token, user_id=creds.get("thr_user_id", ""))
            try:
                r = await client.create_thread(text)
            finally:
                await client.close()
            if r and r.get("id"):
                result.update(success=True, external_id=r["id"], external_url=r.get("url", ""))
            else:
                result["error"] = ("Threads rejected the post — the token likely needs the "
                                    "threads_content_publish permission (check the app / logs)")

        elif platform == "tw":
            from clients.tw.client import TWClient
            at = creds.get("tw_auth_token", "")
            ct0 = creds.get("tw_ct0", "")
            if not (at and ct0):
                result["error"] = "X/Twitter account isn't connected"
                return result
            client = TWClient(auth_token=at, ct0=ct0, target_user=creds.get("tw_target_user", ""))
            try:
                media_ids: list[str] = []
                for pth in image_paths:
                    mid = await client.upload_media(pth)
                    if not mid:
                        result["error"] = ("X rejected the image upload — the media endpoint may "
                                            "have moved or the cookie session lacks upload rights "
                                            "(check logs)")
                        return result
                    media_ids.append(mid)
                r = await client.create_tweet(text, media_ids=media_ids or None)
            finally:
                await client.close()
            if r and r.get("id"):
                result.update(success=True, external_id=r["id"], external_url=r.get("url", ""))
            else:
                result["error"] = ("X rejected the post — the cookie session may be expired, or "
                                    "the CreateTweet query id/features need refreshing (check logs)")

        elif platform == "tum":
            from clients.tum.client import TumClient
            key = creds.get("tum_api_key", "")
            blog = creds.get("tum_blog", "")
            cs = creds.get("tum_consumer_secret", "")
            ot = creds.get("tum_oauth_token", "")
            ots = creds.get("tum_oauth_token_secret", "")
            if not (key and blog and cs and ot and ots):
                result["error"] = ("Tumblr posting needs OAuth1 tokens — add the consumer secret, "
                                    "OAuth token and token secret in the Tumblr settings")
                return result
            client = TumClient(api_key=key, blog=blog, consumer_secret=cs,
                               oauth_token=ot, oauth_token_secret=ots)
            try:
                r = await client.create_text_post(text)
            finally:
                await client.close()
            if r and r.get("id"):
                result.update(success=True, external_id=r["id"], external_url=r.get("url", ""))
            else:
                result["error"] = "Tumblr rejected the post (check the OAuth1 tokens / logs)"

        elif platform == "ig":
            token = creds.get("ig_access_token", "")
            if not token:
                result["error"] = "Instagram account isn't connected"
                return result
            # Instagram fetches the image from a public URL (it never accepts
            # bytes). Two ways to give it one:
            #  • Server:  IG_PUBLIC_BASE_URL is set → stash locally + serve it.
            #  • Desktop: no public address, but paired with a server → relay each
            #    image to that server's /api/ig/pubmedia and use the URL it returns.
            s = settings or config.get_settings()
            local_base = s.get("ig_public_base_url", "").strip()
            relay_url = s.get("posting_server_url", "").strip()
            relay_key = s.get("posting_server_api_key", "").strip()
            if not local_base and not relay_url:
                result["error"] = ("Instagram posting needs a public address for Meta to fetch the "
                                   "image. On the server, set IG_PUBLIC_BASE_URL. On the desktop app, "
                                   "pair it with your server (Settings → Posting: server URL + API "
                                   "key) so it can hand the image to the server for hosting.")
                return result
            from posting import ig_media
            from clients.ig.client import IgClient
            stashed: list[str] = []      # only LOCAL stashes are cleaned up here
            try:
                image_urls: list[str] = []
                if local_base:
                    # Stash each image publicly on this server (Meta cURLs it).
                    for pth in image_paths:
                        t = ig_media.stash_image(pth)
                        stashed.append(t)
                        image_urls.append(ig_media.public_url(local_base, t))
                else:
                    # Desktop: relay each image to the paired server (it TTL-sweeps
                    # its own stashes, so nothing to clean up from here).
                    for pth in image_paths:
                        image_urls.append(await _relay_stash_image(relay_url, relay_key, pth))
                client = IgClient(access_token=token, user_id=creds.get("ig_user_id", ""))
                try:
                    r = await client.create_post(text, image_urls)
                finally:
                    await client.close()
                if r and r.get("id"):
                    result.update(success=True, external_id=r["id"], external_url=r.get("url", ""))
                else:
                    result["error"] = "Instagram rejected the post (check the token / logs)"
            finally:
                for t in stashed:
                    ig_media.cleanup(t)

        elif platform == "tg":
            from clients.tg.client import TgClient
            # Reuse the notification bot token if a posting-specific one isn't set,
            # so a user who already connected Telegram for alerts only needs to add
            # the channel + make the bot an admin of it.
            s = settings or config.get_settings()
            token = creds.get("tg_bot_token", "") or s.get("telegram_bot_token", "")
            channel = creds.get("tg_channel", "")
            if not token:
                result["error"] = ("Telegram bot token isn't set — connect Telegram (Settings → "
                                   "Telegram channel), or reuse your notification bot")
                return result
            if not channel:
                result["error"] = "No Telegram channel set — add your @channel in the Telegram settings"
                return result
            client = TgClient(bot_token=token, channel=channel)
            r = await client.create_post(
                text, image_paths=image_paths,
                spoiler=(rating in _SENSITIVE_RATINGS))
            if r and r.get("id"):
                result.update(success=True, external_id=r["id"],
                              external_url=r.get("url", ""))
            else:
                result["error"] = ("Telegram rejected the post — check the bot is an admin of the "
                                   "channel and the token/channel are correct (see logs)")
    except Exception as e:
        logger.error("Post publish to %s failed: %s", platform, e, exc_info=True)
        result["error"] = str(e)
    return result


async def _publish_thread_parts(parts: list[dict], platform: str,
                                account_id: int | None, settings: dict | None,
                                parent_res: dict) -> list[dict]:
    """Post thread parts as a reply chain (gap-wave-3 §4). bsky + mast only —
    each part replies to the previous; part refs come from the client returns
    (bsky uri+cid via parent_res["_refs"], mast numeric id via external_id).
    One result dict per part; a failed part stops the chain (no orphaned tails).
    """
    out: list[dict] = []
    account_id, creds = _resolve_creds(platform, account_id, settings)
    if platform == "bsky":
        from clients.bsky.client import BskyClient
        root = parent_res.get("_refs") or {}
        if not (root.get("uri") and root.get("cid")):
            return out
        client = BskyClient(identifier=creds.get("bsky_identifier", ""),
                            app_password=creds.get("bsky_app_password", ""))
        prev = dict(root)
        try:
            for part in parts:
                r = await client.create_post(part["body"], reply={
                    "root": {"uri": root["uri"], "cid": root["cid"]},
                    "parent": {"uri": prev["uri"], "cid": prev["cid"]},
                })
                res = {"platform": platform, "account_id": account_id,
                       "success": bool(r and r.get("uri")), "part": part["post_id"],
                       "external_id": (r or {}).get("uri", ""),
                       "external_url": (r or {}).get("url", ""),
                       "error": "" if r else "Bluesky rejected a thread part"}
                out.append(res)
                if not res["success"]:
                    break
                prev = {"uri": r["uri"], "cid": r.get("cid", "")}
        finally:
            await client.close()
    elif platform == "mast":
        from clients.mast.client import MastClient
        prev_id = parent_res.get("external_id", "")
        if not prev_id:
            return out
        client = MastClient(instance_url=creds.get("mast_instance_url", ""),
                            access_token=creds.get("mast_access_token", ""))
        try:
            for part in parts:
                r = await client.create_status(
                    part["body"], in_reply_to_id=str(prev_id),
                    idempotency_key=f"pp-{part['post_id']}-mast")
                res = {"platform": platform, "account_id": account_id,
                       "success": bool(r and r.get("id")), "part": part["post_id"],
                       "external_id": str((r or {}).get("id", "")),
                       "external_url": (r or {}).get("url", ""),
                       "error": "" if r else "Mastodon rejected a thread part"}
                out.append(res)
                if not res["success"]:
                    break
                prev_id = r["id"]
        finally:
            await client.close()
    return out


async def publish_post(post_id: int, platforms: list[str],
                       account_ids: dict[str, int] | None = None,
                       settings: dict | None = None) -> list[dict[str, Any]]:
    """Publish a composed post to each platform, recording every outcome.

    Returns one result dict per platform. Each publication row is upserted so a
    re-publish of a failed platform overwrites its prior failure.
    """
    account_ids = account_ids or {}
    conn = get_connection()
    try:
        post = posts_queries.get_post(conn, post_id)
    finally:
        conn.close()
    if not post:
        raise ValueError(f"post {post_id} not found")

    results: list[dict[str, Any]] = []
    for platform in platforms:
        res = await _publish_one(post, platform, account_ids.get(platform), settings)
        results.append(res)
        conn = get_connection()
        try:
            posts_queries.upsert_post_publication(
                conn, post_id=post_id, platform=platform, account_id=res["account_id"],
                status="posted" if res["success"] else "failed",
                external_id=res.get("external_id", ""),
                external_url=res.get("external_url", ""),
                error=res.get("error", ""), now=_now(),
            )
        finally:
            conn.close()

    # Thread parts (gap-wave-3 §4): chain replies on bsky/mast after the parent
    # posted; other platforms get the parent only + a note. Each part records
    # its own publication row (part post_ids are real post rows).
    conn = get_connection()
    try:
        parts = posts_queries.get_thread_parts(conn, post_id)
    finally:
        conn.close()
    if parts:
        for res in list(results):
            if not res.get("success"):
                continue
            plat = res["platform"]
            if plat in ("bsky", "mast"):
                part_results = await _publish_thread_parts(
                    parts, plat, account_ids.get(plat), settings, res)
                conn = get_connection()
                try:
                    for pr in part_results:
                        posts_queries.upsert_post_publication(
                            conn, post_id=pr["part"], platform=plat,
                            account_id=pr["account_id"],
                            status="posted" if pr["success"] else "failed",
                            external_id=pr.get("external_id", ""),
                            external_url=pr.get("external_url", ""),
                            error=pr.get("error", ""), now=_now())
                finally:
                    conn.close()
                ok = sum(1 for pr in part_results if pr["success"])
                res["thread_parts"] = f"{ok}/{len(parts)} parts posted"
                if ok < len(parts):
                    res["error"] = (res.get("error") or "") or "some thread parts failed"
            else:
                res["thread_parts"] = f"first part only ({plat} threads unsupported)"

    # Discord announce (gap G4) — fire once per publish if any platform succeeded.
    # Best-effort; announce_publish self-gates on config + never raises.
    succeeded = [platforms[i] for i, r in enumerate(results) if r.get("success")]
    if succeeded:
        from posting import discord
        first_url = next((r.get("external_url") for r in results
                          if r.get("success") and r.get("external_url")), None)
        await discord.announce_publish(
            kind="post",
            title=" ".join((post.get("body") or "").split())[:80] or "New post",
            url=first_url, rating=post.get("rating"), platforms=succeeded,
        )
    return results
