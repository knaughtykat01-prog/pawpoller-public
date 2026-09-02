"""Instagram (IG) Graph-API client — analytics only.

Instagram has an OFFICIAL API (graph.instagram.com, the "Instagram API with
Instagram Login" flow — no Facebook Page required). Auth is OAuth2 — the user
supplies a **long-lived Instagram user access token** from a Meta app with the
``instagram_business_basic`` + ``instagram_business_manage_insights`` scopes,
generated for a **Business/Creator** account. The token is refreshed best-effort
on connect (long-lived tokens last ~60 days and can be extended).

Mirrors the Threads client (clients/thr/client.py) — same Meta-Graph shape — with
Instagram's specifics:
  - Media IDs are numeric strings, stored as TEXT.
  - like_count / comments_count come straight off the media object (no insights
    call needed for those); views / reach / saved / shares come from the per-media
    /insights endpoint (one call per post — no batch).
  - ``impressions`` was deprecated for media created after 2024-07-02; ``views``
    is its replacement, so we track views (not impressions).
  - content_type is derived from media_type (image / video / carousel) with
    media_product_type == REELS → reel.
  - Pagination: paging.next (full URL) on the /media edge.

NOTE: Meta gates this behind a Business/Creator account + app review and removes
adult content, so it may be unusable for some accounts. The client is built to
the documented API; live behaviour depends on the user's Meta app + token.
(The Threads sibling was live-verified end-to-end 2026-07-10.)
"""

from __future__ import annotations
import asyncio
import logging
from typing import Any

import httpx

import config

logger = logging.getLogger(__name__)

_API_BASE = "https://graph.instagram.com/v21.0"
_REFRESH_URL = "https://graph.instagram.com/refresh_access_token"

_HEADERS = {
    "User-Agent": "PawPoller/1.0",
    "Accept": "application/json",
}

# media_type / media_product_type → our content_type badge.
_MEDIA_TYPE_MAP = {
    "IMAGE": "image",
    "VIDEO": "video",
    "CAROUSEL_ALBUM": "carousel",
}

# Per-media insight metrics we request. likes/comments come off the media object
# instead (reliable across all media types); these add reach/saves/shares/views.
# Kept conservative so the call stays valid for feed images, videos and carousels.
_INSIGHT_METRICS = "views,reach,saved,shares"

_MEDIA_FIELDS = (
    "id,caption,media_type,media_product_type,permalink,timestamp,"
    "thumbnail_url,media_url,like_count,comments_count,username,"
    # Carousel children — each image/video in an album — so all-images import
    # can bring in the whole set (field is simply absent for single-media posts).
    "children{media_url,media_type}"
)


def _safe_int(val: Any) -> int:
    if val is None:
        return 0
    try:
        if isinstance(val, str):
            val = val.replace(",", "").strip()
        return int(val)
    except (ValueError, TypeError):
        return 0


class IgAuthError(Exception):
    """An Instagram auth failure that is NOT a plainly expired/invalid token.

    Meta's Graph API returns an OAuthException ``code`` for auth problems:
      - **190** = the access token is expired/invalid → the user really does
        need to re-enter it. ``validate_session()`` signals that by returning
        ``None`` (its historical "not alive" contract).
      - **200 "API access blocked"**, other permission errors, and rate limits
        are *app-level / transient* — the token itself may be perfectly fine.
        Reporting those as "expired — re-enter credentials" sends the user
        chasing the wrong fix, so ``validate_session()`` raises THIS instead.
        ``polling/session_check`` turns a raise into an amber "couldn't verify"
        state (with this message), distinct from a red "expired".
    """


class IgClient:
    """Async client for the official Instagram Graph API (analytics only)."""

    def __init__(self, access_token: str = "", user_id: str = "",
                 proxy_url: str = "", proxy_key: str = ""):
        self.access_token = access_token
        self.user_id = str(user_id or "")     # numeric IG user id; "" → resolve "me"
        self._username: str = ""
        self._logged_in = False

        if proxy_url and proxy_key:
            from polling.cf_proxy import CloudflareProxyTransport
            transport = CloudflareProxyTransport(proxy_url, proxy_key)
            logger.info("Ig client using CF proxy: %s", proxy_url)
        else:
            transport = httpx.AsyncHTTPTransport(retries=2)
        self._http = httpx.AsyncClient(
            timeout=30.0,
            follow_redirects=True,
            headers=_HEADERS,
            transport=transport,
        )

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        await self.close()

    async def close(self) -> None:
        await self._http.aclose()

    def update_credentials(self, access_token: str, user_id: str = "") -> None:
        new_uid = str(user_id or "")
        changed = (self.access_token != access_token or self.user_id != new_uid)
        self.access_token = access_token
        self.user_id = new_uid
        if changed:
            self._logged_in = False
            self._username = ""

    # -- Auth -----------------------------------------------------------------

    async def _try_refresh(self) -> None:
        """Best-effort long-lived-token refresh (extends expiry). A fresh token
        (<24h old) can't be refreshed yet — that's fine, we ignore the error."""
        try:
            resp = await self._http.get(_REFRESH_URL, params={
                "grant_type": "ig_refresh_token",
                "access_token": self.access_token,
            })
            if resp.status_code == 200:
                tok = resp.json().get("access_token")
                if tok:
                    self.access_token = tok   # rotate; connect route persists it
        except Exception:
            logger.debug("IG: token refresh skipped", exc_info=True)

    async def validate_session(self) -> str | None:
        """Resolve the account (and refresh the token).

        Returns the username on a live token; returns ``None`` ONLY for a
        genuinely expired/invalid token (Meta OAuthException code 190). For any
        OTHER auth failure — most importantly code 200 "API access blocked" (an
        app-level block on the user's Meta app, not a dead token) or a rate
        limit — it raises :class:`IgAuthError` so the caller can report the real
        reason instead of falsely telling the user to re-enter a perfectly good
        access token. (Uses a raw request rather than ``_get_json`` so it can
        see the Meta error ``code`` that ``_get_json`` swallows.)"""
        if not self.access_token:
            return None
        await self._try_refresh()
        try:
            resp = await self._http.get(f"{_API_BASE}/me", params={
                "fields": "user_id,username",
                "access_token": self.access_token,
            })
        except httpx.HTTPError as e:
            raise IgAuthError(f"Network error contacting Instagram: {e}") from e

        if resp.status_code == 200:
            data = resp.json() if resp.content else {}
            if isinstance(data, dict):
                uid = data.get("user_id") or data.get("id")
                if uid:
                    if not self.user_id:
                        self.user_id = str(uid)
                    self._username = data.get("username", "") or self._username
                    self._logged_in = True
                    return self._username or self.user_id
            return None

        # Non-200 → classify the Meta OAuthException code.
        err = {}
        try:
            err = (resp.json() or {}).get("error", {}) or {}
        except Exception:
            pass
        code = err.get("code")
        msg = err.get("message") or f"HTTP {resp.status_code}"
        if code == 190:
            # Genuinely expired / invalidated token → "re-enter credentials"
            # is the right advice; signal it the historical way (None).
            logger.info("IG: access token expired/invalid (code 190): %s", msg)
            return None
        # code 200 "API access blocked", other permission errors, rate limits:
        # NOT an expired token — surface the real reason.
        logger.error("IG: non-expiry auth failure (code %s): %s", code, msg)
        raise IgAuthError(
            f"Meta blocked Instagram API access (code {code}: {msg}). This is "
            f"an app-level block, not an expired token — check your Meta app's "
            f"status and permissions rather than re-entering the token.")

    async def ensure_logged_in(self) -> bool:
        if self._logged_in and self.user_id:
            return True
        return bool(await self.validate_session())

    # -- HTTP Helpers ---------------------------------------------------------

    async def _get_json(self, url: str, params: dict | None = None) -> dict | None:
        params = dict(params or {})
        params.setdefault("access_token", self.access_token)
        try:
            resp = await self._http.get(url, params=params)
            if resp.status_code == 429:
                logger.warning("IG: Rate limited (429), waiting 30s...")
                await asyncio.sleep(30)
                resp = await self._http.get(url, params=params)
            if resp.status_code in (400, 401):
                logger.error("IG: auth error (%s): %s", resp.status_code, resp.text[:200])
                return None
            if resp.status_code == 404:
                logger.warning("IG: Not found (404) for %s", url)
                return None
            resp.raise_for_status()
            return resp.json()
        except httpx.HTTPError as e:
            logger.error("IG: Failed to fetch %s: %s", url, e)
            return None
        except Exception as e:
            logger.error("IG: JSON parse error for %s: %s", url, e)
            return None

    # -- Post Discovery -------------------------------------------------------

    async def get_all_post_uris(self) -> list[dict]:
        """Page through the user's media. Items carry the post metadata (including
        like_count/comments_count); reach/saves/shares/views come per-post in the
        details pass."""
        if not await self.ensure_logged_in():
            logger.error("IG: Not logged in, cannot fetch media")
            return []

        all_posts: list[dict] = []
        seen: set[str] = set()
        url = f"{_API_BASE}/{self.user_id}/media"
        params: dict | None = {"fields": _MEDIA_FIELDS, "limit": "100"}

        for _page_safety in range(1000):
            data = await self._get_json(url, params)
            if not data or not isinstance(data, dict):
                break
            posts = data.get("data") or []
            for post in posts:
                pid = str(post.get("id", ""))
                if not pid or pid in seen:
                    continue
                seen.add(pid)
                all_posts.append({"post_uri": pid, "post": post})

            next_url = (data.get("paging") or {}).get("next")
            if not next_url or not posts:
                break
            url, params = next_url, None   # next carries all params
            await asyncio.sleep(config.IG_REQUEST_DELAY_SECONDS)

        logger.info("IG: Found %d media for %s", len(all_posts), self._username or self.user_id)
        return all_posts

    # -- Post Details ---------------------------------------------------------

    async def _get_insights(self, media_id: str) -> dict:
        """Fetch per-post insights. Returns {views, reach, saved, shares}.

        Instagram's insights endpoint 400s the whole call if a metric is invalid
        for that media type, so on any error we just return zeroes — the likes and
        comments (the core counts) still come from the media object regardless."""
        data = await self._get_json(
            f"{_API_BASE}/{media_id}/insights",
            {"metric": _INSIGHT_METRICS},
        )
        out = {"views": 0, "reach": 0, "saved": 0, "shares": 0}
        if data and isinstance(data, dict):
            for m in data.get("data", []) or []:
                name = m.get("name")
                if name not in out:
                    continue
                # Instagram returns values[].value for media insights.
                if "total_value" in m:
                    out[name] = _safe_int((m.get("total_value") or {}).get("value", 0))
                else:
                    vals = m.get("values") or []
                    out[name] = _safe_int(vals[0].get("value", 0)) if vals else 0
        return out

    async def get_post_details_batch(self, items: list[dict]) -> list[dict]:
        """One insights call per post (Instagram has no batch insights endpoint)."""
        details: list[dict] = []
        for i, item in enumerate(items):
            post = item.get("post") or {}
            uri = item.get("post_uri", "")
            if i > 0:
                await asyncio.sleep(config.IG_REQUEST_DELAY_SECONDS)
            try:
                insights = await self._get_insights(uri) if uri else {}
            except Exception as e:
                logger.warning("IG: insights failed for %s: %s", uri, e)
                insights = {}
            details.append(self._parse_post(post, insights))
        return details

    # -- Parsing Helpers ------------------------------------------------------

    def _parse_post(self, post: dict, insights: dict) -> dict:
        uri = str(post.get("id", ""))
        caption = post.get("caption", "") or ""
        media_type = post.get("media_type", "")
        if (post.get("media_product_type") or "").upper() == "REELS":
            content_type = "reel"
        else:
            content_type = _MEDIA_TYPE_MAP.get(media_type, "image")
        thumbnail_url = post.get("thumbnail_url", "") or post.get("media_url", "") or ""

        # Full image set: a carousel album exposes its children — collect every
        # IMAGE child's media_url so the artwork importer brings them all in
        # (video children are skipped; not importable images). A single-media
        # post has no `children`, so media_urls stays empty and the importer
        # falls back to the one media_url/thumbnail (unchanged behaviour).
        media_urls: list[str] = []
        children = ((post.get("children") or {}).get("data")) or []
        if children:
            media_urls = [c.get("media_url", "") for c in children
                          if (c.get("media_type") or "").upper() == "IMAGE" and c.get("media_url")]

        return {
            "post_uri": uri,
            "title": caption[:80] + ("..." if len(caption) > 80 else "") if caption else "(no caption)",
            "full_text": caption,
            "username": post.get("username", self._username),
            "posted_at": post.get("timestamp", ""),
            "content_type": content_type,
            "rating": "General",
            "description": caption,
            "keywords": [],
            "link": post.get("permalink", ""),
            "thumbnail_url": thumbnail_url,
            "media_urls": media_urls,
            "views": _safe_int(insights.get("views", 0)),
            "reach": _safe_int(insights.get("reach", 0)),
            "likes": _safe_int(post.get("like_count", 0)),
            "comments": _safe_int(post.get("comments_count", 0)),
            "saved": _safe_int(insights.get("saved", 0)),
            "shares": _safe_int(insights.get("shares", 0)),
            "has_media": 1 if thumbnail_url else 0,
            "embed_type": media_type,
        }

    def _empty_detail(self, uri: str) -> dict:
        return {
            "post_uri": uri, "title": "", "full_text": "", "username": self._username,
            "posted_at": "", "content_type": "image", "rating": "General",
            "description": "", "keywords": [], "link": "", "thumbnail_url": "", "media_urls": [],
            "views": 0, "reach": 0, "likes": 0, "comments": 0, "saved": 0, "shares": 0,
            "has_media": 0, "embed_type": "",
        }

    # -- Posting (Content Publishing) -----------------------------------------
    #
    # Instagram publishes in two steps: create a media *container* (Meta cURLs
    # the public image_url and processes it) then publish the container. A
    # carousel wraps 2-10 child containers. Every post REQUIRES media — there is
    # no text-only Instagram post. Needs the instagram_business_content_publish
    # scope + a Business/Creator account.

    async def _post_json(self, url: str, data: dict) -> dict | None:
        """POST form-encoded params, raising a RuntimeError with Meta's own error
        message on failure (so the publisher can surface it to the user)."""
        payload = dict(data)
        payload.setdefault("access_token", self.access_token)
        try:
            resp = await self._http.post(url, data=payload)
            if resp.status_code == 429:
                logger.warning("IG: Rate limited (429) on POST, waiting 30s...")
                await asyncio.sleep(30)
                resp = await self._http.post(url, data=payload)
            if resp.status_code >= 400:
                try:
                    err = (resp.json() or {}).get("error", {})
                    msg = err.get("error_user_msg") or err.get("message") or resp.text[:200]
                except Exception:
                    msg = resp.text[:200]
                logger.error("IG: POST %s error (%s): %s", url, resp.status_code, msg)
                raise RuntimeError(f"Instagram API error ({resp.status_code}): {msg}")
            return resp.json()
        except httpx.HTTPError as e:
            raise RuntimeError(f"Instagram request failed: {e}")

    async def _create_container(self, caption: str | None = None, image_url: str | None = None,
                                is_carousel_item: bool = False, media_type: str | None = None,
                                children: list[str] | None = None) -> str:
        data: dict[str, str] = {}
        if image_url:
            data["image_url"] = image_url
        if caption is not None:
            data["caption"] = caption
        if is_carousel_item:
            data["is_carousel_item"] = "true"
        if media_type:
            data["media_type"] = media_type
        if children:
            data["children"] = ",".join(children)
        res = await self._post_json(f"{_API_BASE}/{self.user_id}/media", data)
        cid = str((res or {}).get("id", ""))
        if not cid:
            raise RuntimeError("Instagram did not return a media container id")
        return cid

    async def _wait_container_ready(self, container_id: str, tries: int = 6) -> None:
        """Poll the container's status_code until FINISHED (images are usually
        instant; videos/carousels take a moment). ERROR/EXPIRED raises."""
        for _ in range(tries):
            data = await self._get_json(f"{_API_BASE}/{container_id}", {"fields": "status_code"})
            status = (data or {}).get("status_code", "")
            if status in ("FINISHED", "PUBLISHED"):
                return
            if status in ("ERROR", "EXPIRED"):
                raise RuntimeError(f"Instagram media container {status.lower()} (image rejected?)")
            await asyncio.sleep(config.IG_REQUEST_DELAY_SECONDS * 2)
        # Not FINISHED after the wait — publish will surface any real problem.

    async def _publish_container(self, creation_id: str) -> dict:
        res = await self._post_json(f"{_API_BASE}/{self.user_id}/media_publish",
                                    {"creation_id": creation_id})
        media_id = str((res or {}).get("id", ""))
        if not media_id:
            raise RuntimeError("Instagram publish did not return a media id")
        permalink = ""
        try:
            d = await self._get_json(f"{_API_BASE}/{media_id}", {"fields": "permalink"})
            permalink = (d or {}).get("permalink", "")
        except Exception:
            pass
        return {"id": media_id, "url": permalink}

    async def create_post(self, caption: str, image_urls: list[str]) -> dict:
        """Publish a photo (or 2-10 photo carousel) with a caption. Returns
        {id, url}. Raises RuntimeError with a user-facing message on failure."""
        if not await self.ensure_logged_in():
            raise RuntimeError("Instagram auth failed — reconnect the account")
        urls = [u for u in (image_urls or []) if u]
        if not urls:
            raise RuntimeError("Instagram requires at least one image")

        if len(urls) == 1:
            container = await self._create_container(caption=caption, image_url=urls[0])
            await self._wait_container_ready(container)
            return await self._publish_container(container)

        # Carousel: create each child, wait, then wrap + publish.
        child_ids: list[str] = []
        for u in urls[:10]:
            child_ids.append(await self._create_container(image_url=u, is_carousel_item=True))
        for cid in child_ids:
            await self._wait_container_ready(cid)
        carousel = await self._create_container(caption=caption, media_type="CAROUSEL",
                                                children=child_ids)
        await self._wait_container_ready(carousel)
        return await self._publish_container(carousel)
