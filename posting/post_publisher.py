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
import unicodedata
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

# ── Per-site rules (spec 018, FR-005) ──────────────────────────────
# The ONE table of what each site takes; the composer is sent it (GET /api/posts/rules)
# and asks `preview` how a draft will come out, so the browser holds no second copy.
LABELS = {"bsky": "Bluesky", "tw": "X", "mast": "Mastodon", "thr": "Threads",
          "tum": "Tumblr", "ig": "Instagram", "tg": "Telegram"}
# Characters per post. Tumblr has no practical limit on a text post.
TEXT_LIMITS = {"bsky": 300, "tw": 280, "mast": 500, "thr": 500, "ig": 2200, "tg": 4096,
               "tum": None}
TG_CAPTION_LIMIT = 1024          # a Telegram post with a picture: the text is its caption
MAX_IMAGES = 4                   # X / Bluesky / Mastodon all cap a post at 4 images
THREAD_PLATFORMS = ("bsky", "mast")             # parts 2+ chain as replies; the rest get part 1
HANDLE_PLATFORMS = ("bsky", "tw", "mast", "thr", "tum")   # a contact can hold a handle there
# The credentials `_publish_one` needs before it can try each site.
_REQUIRED_CREDS = {
    "bsky": ("bsky_identifier", "bsky_app_password"),
    "mast": ("mast_instance_url", "mast_access_token"),
    "thr": ("thr_access_token",),
    "tw": ("tw_auth_token", "tw_ct0"),
    "tum": ("tum_api_key", "tum_blog", "tum_consumer_secret", "tum_oauth_token",
            "tum_oauth_token_secret"),
    "ig": ("ig_access_token",),
    "tg": ("tg_bot_token", "tg_channel"),
}


def text_limit(platform: str, image_count: int = 0) -> int | None:
    if platform == "tg" and image_count:
        return TG_CAPTION_LIMIT
    return TEXT_LIMITS.get(platform)


def text_length(text: str) -> int:
    """Characters as a reader counts them: an emoji with a skin tone, a ZWJ family, a
    flag or an accented letter is one. No dependency, so it approximates grapheme
    clusters by folding the joiners and modifiers into the character before them.
    ponytail: X weighs some characters double (CJK, emoji) and every link as 23; this
    counts them as Bluesky does, so a post near X's limit may still be refused there."""
    n, join, ri_open = 0, False, False
    for ch in text or "":
        cp = ord(ch)
        if join:                       # the character after a zero-width joiner
            join = False
            continue
        if cp == 0x200D:
            join = True
            continue
        if (unicodedata.combining(ch) or 0xFE00 <= cp <= 0xFE0F or 0x1F3FB <= cp <= 0x1F3FF
                or 0xE0020 <= cp <= 0xE007F):
            continue
        if 0x1F1E6 <= cp <= 0x1F1FF:   # regional indicators pair into one flag
            if ri_open:
                ri_open = False
                continue
            ri_open = True
        else:
            ri_open = False
        n += 1
    return n


def rules() -> dict:
    from posting import paired_comment
    return {"labels": LABELS, "limits": TEXT_LIMITS, "tg_caption_limit": TG_CAPTION_LIMIT,
            "max_images": MAX_IMAGES, "text_only": list(_TEXT_ONLY),
            "image_required": list(_IMAGE_REQUIRED), "thread_platforms": list(THREAD_PLATFORMS),
            "handle_platforms": list(HANDLE_PLATFORMS),
            # Spec 021: where a paired comment can go, and what a template may say.
            "reply_platforms": list(paired_comment.REPLY_PLATFORMS),
            "proven_reply_platforms": sorted(paired_comment.PROVEN),
            "comment_placeholders": list(paired_comment.PLACEHOLDERS)}


def comment_preview(plat: str, comment, mentions: list[dict], ctx: dict | None,
                    settings: dict | None, *, blocked_account: bool = False,
                    piece: bool = False) -> dict:
    """One site's paired comment as it will go out, and every reason it won't (spec 021
    FR-004). `piece`: a link made later in the same publish may still fill `{link}`, so
    an empty token is a warning there, a block on a composed post."""
    from posting import paired_comment as pc
    text = comment.get("text") if isinstance(comment, dict) else comment
    text = str(text or "")
    label = LABELS.get(plat, plat)
    warns: list[dict] = []
    if plat not in pc.REPLY_PLATFORMS:
        return {"text": "", "length": 0, "limit": None, "over": False,
                "warnings": [{"level": "warn", "text": f"{label}: no replies there, the comment will be left off"}]}
    bad = pc.unknown_tokens(text)
    if bad:
        warns.append({"level": "block", "text": f"{{{bad[0]}}} isn't a placeholder PawPoller knows"})
    filled, empty = pc.fill(text, ctx, plat, settings)
    for t in empty:
        warns.append({"level": "warn", "text": f"{{{t}}} (filled from this publish, if that site posts first)"}
                     if piece else {"level": "block", "text": f"{{{t}}} has nothing to fill it"})
    rendered = _render_body(filled, mentions, plat)
    limit = pc.comment_limit(plat)
    length = text_length(rendered)
    over = bool(limit) and length > limit
    if over:
        warns.append({"level": "block", "text": f"Comment is {length - limit} over {label}'s {limit}-character limit"})
    if blocked_account:
        warns.append({"level": "block", "text": accounts_db.NEVER_POST_ERROR})
    if plat not in pc.PROVEN:
        warns.append({"level": "warn", "text": f"Comments on {label} aren't proven live yet"})
    return {"text": rendered, "length": length, "limit": limit, "over": over, "warnings": warns}


def _mentions_for(conn, bindings) -> list[dict]:
    """Composer bindings [{token, contact_id}] → the mention dicts `_render_body` reads."""
    out = []
    for b in bindings or []:
        try:
            cid = int(b.get("contact_id") or 0)
        except (TypeError, ValueError, AttributeError):
            continue
        token = str(b.get("token") or "").strip().lstrip("@")
        c = posts_queries.get_contact(conn, cid) if cid and token else None
        if c:
            out.append({"token": token, **{f"handle_{p}": c.get(f"handle_{p}", "")
                                            for p in HANDLE_PLATFORMS}})
    return out


def preview(body: str, platforms: list[str], bindings=None, image_count: int = 0,
            parts=None, account_ids: dict | None = None, settings: dict | None = None,
            comments: dict | None = None, linked: dict | None = None) -> dict:
    """How a draft will come out on each chosen site, and every reason a site would refuse
    or change it — before anything is sent (spec 018, FR-004 / SC-002).

    Uses the publisher's own `_render_body`, the same table of limits and media rules, and
    the same credential lookup `_publish_one` makes. Warning levels: "block" means the site
    will refuse it as written; "warn" means it goes out, changed."""
    account_ids = account_ids or {}
    parts = [str(p) for p in (parts or []) if str(p).strip()]
    conn = get_connection()
    try:
        mentions = _mentions_for(conn, bindings)
    finally:
        conn.close()
    never = accounts_db.never_post_ids(settings)
    out: dict[str, dict] = {}
    for plat in platforms:
        if plat not in SUPPORTED:
            continue
        label = LABELS.get(plat, plat)
        text = _render_body(body or "", mentions, plat)
        limit = text_limit(plat, image_count)
        length = text_length(text)
        warns: list[dict] = []

        def w(level, msg):
            warns.append({"level": level, "text": msg})

        acct_id, creds = _resolve_creds(plat, account_ids.get(plat), settings)
        connected = all(creds.get(k) for k in _REQUIRED_CREDS.get(plat, ()))
        if not connected:
            w("block", f"{label} isn't connected")
        elif acct_id in never:
            w("block", accounts_db.NEVER_POST_ERROR)
        over = bool(limit) and length > limit
        if over:
            w("block", f"{length - limit} over {label}'s {limit}-character limit")
        if plat in _TEXT_ONLY and image_count:
            s = "" if image_count == 1 else "s"
            w("warn", f"Text only: your {image_count} image{s} will be left off")
        if plat in _IMAGE_REQUIRED and not image_count:
            w("block", f"{label} needs a photo")
        if parts:
            if plat in THREAD_PLATFORMS:
                for i, part in enumerate(parts):
                    n = text_length(part)
                    if limit and n > limit:
                        w("block", f"Part {i + 2} is {n - limit} over the {limit}-character limit")
            else:
                w("warn", f"{label} gets part 1 only (no threads there)")
        if plat in HANDLE_PLATFORMS:
            for m in mentions:
                if not (m.get(f"handle_{plat}") or "").strip():
                    w("warn", f"@{m['token']} has no {label} handle saved, so it posts as plain text")
        out[plat] = {"text": text, "length": length, "limit": limit, "over": over,
                     "connected": connected, "warnings": warns}
        c = (comments or {}).get(plat)
        ctext = c.get("text") if isinstance(c, dict) else c
        if ctext and str(ctext).strip():
            cmentions = mentions
            if isinstance(c, dict) and c.get("mentions"):
                conn = get_connection()
                try:
                    cmentions = _mentions_for(conn, c.get("mentions"))
                finally:
                    conn.close()
            out[plat]["comment"] = comment_preview(
                plat, c, cmentions, _linked_ctx(linked, plat), settings,
                blocked_account=connected and acct_id in never)
    return out


def _linked_ctx(linked: dict | None, platform: str) -> dict | None:
    """Placeholder context from the piece a composed post is about (spec 021)."""
    if not isinstance(linked, dict) or linked.get("kind") not in ("artwork", "story") or not linked.get("ref"):
        return None
    from posting import paired_comment
    return paired_comment.piece_context(linked["kind"], str(linked["ref"]), platform)


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
        # 0 is the legacy "unset" (old rows): resolve it like None, so the never-post check
        # below sees the real default account's id rather than a 0 nothing matches
        # (CMTNEVERPOST0, 4.56.2 — every caller of this, posts and comments, goes through here).
        if not account_id:
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
        # Spec 018: the text goes out and the images are left off — the composer says so
        # on that site's row before posting ("Text only: your 2 images will be left off").
        image_paths, image_alts = [], []

    if platform in _IMAGE_REQUIRED and not image_paths:
        result["error"] = ("Instagram requires a photo — attach an image "
                           "(Instagram has no text-only posts)")
        return result

    account_id, creds = _resolve_creds(platform, account_id, settings)
    result["account_id"] = account_id
    if account_id in accounts_db.never_post_ids(settings):          # FRIENDGUARD (4.45.4)
        result["error"] = accounts_db.NEVER_POST_ERROR
        return result

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
                        result["error"] = client.last_error or (
                            "X rejected the image upload — the media endpoint may "
                            "have moved or the cookie session lacks upload rights "
                            "(check logs)")
                        return result
                    media_ids.append(mid)
                # Flag adult media (4.34.0, TWSENS). Every other platform on this path
                # already reads the post's rating — Bluesky's label, Mastodon's
                # sensitive, Telegram's spoiler — and X was the one that did not, so
                # every microblog post went out unflagged whatever its rating said.
                # An unflagged adult image is how an ACCOUNT gets restricted rather than
                # how a post gets refused: it fails later, elsewhere, and quietly.
                r = await client.create_tweet(text, media_ids=media_ids or None,
                                              sensitive=(rating in _SENSITIVE_RATINGS))
            finally:
                await client.close()
            if r and r.get("id"):
                result.update(success=True, external_id=r["id"], external_url=r.get("url", ""))
            else:
                # What X actually said, not what we guess it means (4.3.4). The
                # old text named two causes and neither was the common one.
                result["error"] = client.last_error or (
                    "X rejected the post and gave no reason (check logs)")

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
            # bytes). posting/ig_host.py climbs the ladder — this instance's public
            # base → paired server → the PawPoller relay → a temporary tunnel —
            # and says exactly what it tried when nothing works.
            s = settings or config.get_settings()
            from posting import ig_host
            from clients.ig.client import IgClient
            hosted = None
            try:
                try:
                    hosted = await ig_host.host_images(list(image_paths), s)
                except ig_host.NoPublicHost as e:
                    result["error"] = str(e)
                    return result
                image_urls = list(hosted.urls)
                logger.info("IG post: image hosted via %s", hosted.how)
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
                if hosted:
                    await hosted.close()

        elif platform == "tg":
            from clients.tg.client import TgClient
            # 4.8.0: channel posting has its own bot; the notification bot is
            # never borrowed (that is how a digest once landed in a channel).
            token = creds.get("tg_bot_token", "")
            channel = creds.get("tg_channel", "")
            if not token:
                result["error"] = ("Telegram channel posting needs its own bot token (Settings → "
                                   "Telegram → Channel posting) — the notification bot is no longer "
                                   "used for channels")
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
    from posting import activity
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
            for k, part in enumerate(parts):
                activity.step(platform, "Thread", f"Part {k + 2} of {len(parts) + 1}")
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
            for k, part in enumerate(parts):
                activity.step(platform, "Thread", f"Part {k + 2} of {len(parts) + 1}")
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
                       settings: dict | None = None,
                       persona_id: int | None = None) -> list[dict[str, Any]]:
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

    from posting import activity   # spec 017: no-ops unless the caller bound a job
    results: list[dict[str, Any]] = []
    for platform in platforms:
        if activity.cancelled():       # "Cancel the rest": stop before the next site
            break
        if platform not in SUPPORTED:
            # 4.52.0 release review: an unknown name is refused WITHOUT a publication row,
            # so a stray string never becomes a stored platform the feed renders.
            results.append({"platform": platform, "account_id": 0, "success": False,
                            "external_id": "", "external_url": "",
                            "error": f"posting to {platform} isn't wired yet", "refused": True})
            continue
        if persona_id is not None:
            # Persona-first: the same refusal manager._resolve_account_id makes,
            # and for the same reason — the platform default may be another
            # persona's account. No publication row is written for a refusal.
            from database import personas as personas_db
            conn = get_connection()
            try:
                err = personas_db.persona_account_error(
                    conn, platform, account_ids.get(platform), persona_id)
            finally:
                conn.close()
            if err:
                results.append({"platform": platform, "account_id": account_ids.get(platform) or 0,
                                "success": False, "external_id": "", "external_url": "",
                                "error": err, "refused": True})
                continue
        activity.step(platform, "Uploading")
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
                # Spec 021: a paired comment goes under the thread's last part that landed.
                last = next((pr for pr in reversed(part_results) if pr["success"]), None)
                if last:
                    res["_last_part"] = last.get("external_id", "")
                res["thread_parts"] = f"{ok}/{len(parts)} parts posted"
                if ok < len(parts):
                    res["error"] = (res.get("error") or "") or "some thread parts failed"
            else:
                res["thread_parts"] = f"first part only ({plat} threads unsupported)"

    # Each site's activity line closes once its thread parts are done too.
    for res in results:
        activity.line_done(res["platform"], bool(res.get("success")), url=res.get("external_url") or "",
                           error=res.get("error") or "" if not res.get("success") else "")

    # Paired comments (spec 021): one pass once every site and thread part is done. A
    # comment reports on its own line and row; it never edits the post's result (FR-006).
    from posting import paired_comment
    try:
        await paired_comment.comment_pass_posts(post, results, settings)
    except Exception:
        logger.error("Paired comment pass failed for post %s", post_id, exc_info=True)

    # Discord announce (gap G4) — fire once per publish if any platform succeeded.
    # Best-effort; announce_publish self-gates on config + never raises.
    succeeded = [platforms[i] for i, r in enumerate(results) if r.get("success")]
    if succeeded:
        from posting import discord
        # 4.41.0 (spec 008): the first image travels with the message, the body is
        # the description, the sites are named links. The title stays the short line.
        media = post.get("media") or []
        if not media and post.get("image_path"):
            media = [{"path": post["image_path"]}]
        first_image = next((m["path"] for m in media if m.get("path")), None)
        await discord.announce_publish(
            kind="post",
            title=" ".join((post.get("body") or "").split())[:80] or "New post",
            rating=post.get("rating"), platforms=succeeded,
            body=post.get("body") or "",
            site_links=[(platforms[i], r["external_url"]) for i, r in enumerate(results)
                        if r.get("success") and r.get("external_url")],
            image_path=first_image,
        )
    return results
