"""Threads (THR) Graph-API client.

Threads (Meta) has an OFFICIAL API (graph.threads.net). Auth is OAuth2 — the
user supplies a **long-lived user access token** from a Meta app with the
``threads_basic`` + ``threads_manage_insights`` scopes. The token is refreshed
best-effort on connect (Threads long-lived tokens last ~60 days and can be
extended).

Key details:
  - Post IDs are numeric strings (the media id), stored as TEXT.
  - Engagement comes from the per-post /insights endpoint:
    views, likes, replies, reposts, quotes (one call per post — no batch).
  - content_type is derived from media_type (text / image / video / carousel),
    with is_quote_post → quote.
  - Pagination: paging.next (full URL) on the threads listing.

NOTE: Meta gates this behind app review and removes adult content, so it may be
unusable for some accounts. The client is built to the documented API; live
behaviour depends on the user's Meta app + token.
"""

from __future__ import annotations
import asyncio
import logging
from typing import Any

import httpx

import config

logger = logging.getLogger(__name__)

_API_BASE = "https://graph.threads.net/v1.0"
_REFRESH_URL = "https://graph.threads.net/refresh_access_token"

_HEADERS = {
    "User-Agent": "PawPoller/1.0",
    "Accept": "application/json",
}

# media_type (from the listing) → our content_type badge.
_MEDIA_TYPE_MAP = {
    "TEXT_POST": "text",
    "IMAGE": "image",
    "VIDEO": "video",
    "CAROUSEL_ALBUM": "carousel",
    "AUDIO": "audio",
    "REPOST_FACADE": "repost",
}


def _safe_int(val: Any) -> int:
    if val is None:
        return 0
    try:
        if isinstance(val, str):
            val = val.replace(",", "").strip()
        return int(val)
    except (ValueError, TypeError):
        return 0


class ThrAuthError(Exception):
    """A Threads auth failure that is NOT a plainly expired/invalid token.

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


class ThrClient:
    """Async client for the official Threads Graph API."""

    def __init__(self, access_token: str = "", user_id: str = "",
                 proxy_url: str = "", proxy_key: str = ""):
        self.access_token = access_token
        self.user_id = str(user_id or "")     # numeric id; "" → resolve "me"
        self._username: str = ""
        self._logged_in = False

        if proxy_url and proxy_key:
            from polling.cf_proxy import CloudflareProxyTransport
            transport = CloudflareProxyTransport(proxy_url, proxy_key)
            logger.info("Thr client using CF proxy: %s", proxy_url)
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
                "grant_type": "th_refresh_token",
                "access_token": self.access_token,
            })
            if resp.status_code == 200:
                tok = resp.json().get("access_token")
                if tok:
                    self.access_token = tok   # rotate; connect route persists it
        except Exception:
            logger.debug("THR: token refresh skipped", exc_info=True)

    async def validate_session(self) -> str | None:
        """Resolve the account (and refresh the token).

        Returns the username on a live token; returns ``None`` ONLY for a
        genuinely expired/invalid token (Meta OAuthException code 190). For any
        OTHER auth failure — most importantly code 200 "API access blocked" (an
        app-level block on the user's Meta app, not a dead token) or a rate
        limit — it raises :class:`ThrAuthError` so the caller can report the
        real reason instead of falsely telling the user to re-enter a perfectly
        good access token. (Uses a raw request rather than ``_get_json`` so it
        can see the Meta error ``code`` that ``_get_json`` swallows.)"""
        if not self.access_token:
            return None
        await self._try_refresh()
        try:
            resp = await self._http.get(f"{_API_BASE}/me", params={
                "fields": "id,username",
                "access_token": self.access_token,
            })
        except httpx.HTTPError as e:
            raise ThrAuthError(f"Network error contacting Threads: {e}") from e

        if resp.status_code == 200:
            data = resp.json() if resp.content else {}
            if isinstance(data, dict) and data.get("id"):
                if not self.user_id:
                    self.user_id = str(data["id"])
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
            logger.info("THR: access token expired/invalid (code 190): %s", msg)
            return None
        # code 200 "API access blocked", other permission errors, rate limits:
        # NOT an expired token — surface the real reason.
        logger.error("THR: non-expiry auth failure (code %s): %s", code, msg)
        raise ThrAuthError(
            f"Meta blocked Threads API access (code {code}: {msg}). This is an "
            f"app-level block, not an expired token — check your Meta app's "
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
                logger.warning("THR: Rate limited (429), waiting 30s...")
                await asyncio.sleep(30)
                resp = await self._http.get(url, params=params)
            if resp.status_code in (400, 401):
                logger.error("THR: auth error (%s): %s", resp.status_code, resp.text[:200])
                return None
            if resp.status_code == 404:
                logger.warning("THR: Not found (404) for %s", url)
                return None
            resp.raise_for_status()
            return resp.json()
        except httpx.HTTPError as e:
            logger.error("THR: Failed to fetch %s: %s", url, e)
            return None
        except Exception as e:
            logger.error("THR: JSON parse error for %s: %s", url, e)
            return None

    async def _post_form(self, url: str, data: dict) -> dict | None:
        payload = dict(data)
        payload["access_token"] = self.access_token
        try:
            resp = await self._http.post(url, data=payload)
            if resp.status_code not in (200, 201):
                logger.error("THR: post failed (%s): %s", resp.status_code, resp.text[:300])
                return None
            return resp.json()
        except Exception as e:
            logger.error("THR: post error for %s: %s", url, e)
            return None

    # -- Posting --------------------------------------------------------------

    async def create_thread(self, text: str) -> dict | None:
        """Publish a text thread (2-step create → publish). Returns {id, url}.

        Text-only: the Graph API pulls images from a PUBLIC ``image_url`` (no file
        upload), which PawPoller doesn't host yet. The access token must carry the
        ``threads_content_publish`` permission — without it publish 400s and this
        returns None (surfaced to the caller as a clear error).
        """
        if not await self.ensure_logged_in():
            return None
        create = await self._post_form(
            f"{_API_BASE}/{self.user_id}/threads",
            {"media_type": "TEXT", "text": text})
        if not create or not create.get("id"):
            return None
        pub = await self._post_form(
            f"{_API_BASE}/{self.user_id}/threads_publish",
            {"creation_id": create["id"]})
        if not pub or not pub.get("id"):
            return None
        media_id = str(pub["id"])
        perma = await self._get_json(f"{_API_BASE}/{media_id}", {"fields": "permalink"})
        return {"id": media_id, "url": (perma or {}).get("permalink", "")}

    # -- Post Discovery -------------------------------------------------------

    async def get_all_post_uris(self) -> list[dict]:
        """Page through the user's threads. Items carry the post metadata; the
        engagement counts are fetched per-post in the details pass."""
        if not await self.ensure_logged_in():
            logger.error("THR: Not logged in, cannot fetch threads")
            return []

        all_posts: list[dict] = []
        seen: set[str] = set()
        url = f"{_API_BASE}/{self.user_id}/threads"
        params: dict | None = {
            "fields": "id,media_type,text,permalink,timestamp,is_quote_post,thumbnail_url,media_url,username",
            "limit": "100",
        }

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
            await asyncio.sleep(config.THR_REQUEST_DELAY_SECONDS)

        logger.info("THR: Found %d threads for %s", len(all_posts), self._username or self.user_id)
        return all_posts

    # -- Post Details ---------------------------------------------------------

    async def _get_insights(self, media_id: str) -> dict:
        """Fetch per-post engagement. Returns {views, likes, replies, reposts, quotes}."""
        data = await self._get_json(
            f"{_API_BASE}/{media_id}/insights",
            {"metric": "views,likes,replies,reposts,quotes"},
        )
        out = {"views": 0, "likes": 0, "replies": 0, "reposts": 0, "quotes": 0}
        if data and isinstance(data, dict):
            for m in data.get("data", []) or []:
                name = m.get("name")
                if name not in out:
                    continue
                # Newer responses use total_value; older use values[].value.
                if "total_value" in m:
                    out[name] = _safe_int((m.get("total_value") or {}).get("value", 0))
                else:
                    vals = m.get("values") or []
                    out[name] = _safe_int(vals[0].get("value", 0)) if vals else 0
        return out

    async def get_post_details_batch(self, items: list[dict]) -> list[dict]:
        """One insights call per post (Threads has no batch insights endpoint)."""
        details: list[dict] = []
        for i, item in enumerate(items):
            post = item.get("post") or {}
            uri = item.get("post_uri", "")
            if i > 0:
                await asyncio.sleep(config.THR_REQUEST_DELAY_SECONDS)
            try:
                insights = await self._get_insights(uri) if uri else {}
            except Exception as e:
                logger.warning("THR: insights failed for %s: %s", uri, e)
                insights = {}
            details.append(self._parse_post(post, insights))
        return details

    # -- Parsing Helpers ------------------------------------------------------

    def _parse_post(self, post: dict, insights: dict) -> dict:
        uri = str(post.get("id", ""))
        text = post.get("text", "") or ""
        media_type = post.get("media_type", "")
        if post.get("is_quote_post"):
            content_type = "quote"
        else:
            content_type = _MEDIA_TYPE_MAP.get(media_type, "text")
        thumbnail_url = post.get("thumbnail_url", "") or post.get("media_url", "") or ""

        return {
            "post_uri": uri,
            "title": text[:80] + ("..." if len(text) > 80 else "") if text else "(no text)",
            "full_text": text,
            "username": post.get("username", self._username),
            "posted_at": post.get("timestamp", ""),
            "content_type": content_type,
            "rating": "General",
            "description": text,
            "keywords": [],
            "link": post.get("permalink", ""),
            "thumbnail_url": thumbnail_url,
            "views": _safe_int(insights.get("views", 0)),
            "likes": _safe_int(insights.get("likes", 0)),
            "reposts": _safe_int(insights.get("reposts", 0)),
            "replies": _safe_int(insights.get("replies", 0)),
            "quotes": _safe_int(insights.get("quotes", 0)),
            "has_media": 1 if thumbnail_url else 0,
            "embed_type": media_type,
        }

    def _empty_detail(self, uri: str) -> dict:
        return {
            "post_uri": uri, "title": "", "full_text": "", "username": self._username,
            "posted_at": "", "content_type": "text", "rating": "General",
            "description": "", "keywords": [], "link": "", "thumbnail_url": "",
            "views": 0, "likes": 0, "reposts": 0, "replies": 0, "quotes": 0,
            "has_media": 0, "embed_type": "",
        }
