"""Pixiv (PIX) app-API client.

Pixiv has no official public API; this uses the same reverse-engineered mobile
("app-api") endpoints the pixivpy library uses. Auth is OAuth2 with a one-time
**refresh token** (obtained via a browser login — e.g. the `gppt` helper) which
is exchanged for a short-lived access token on each session.

Key details:
  - Work IDs are numeric strings; illustrations and novels share the same
    engagement shape, so both are tracked (content_type = illust / manga /
    ugoira / novel).
  - Metrics map to the gallery shape: total_view → views,
    total_bookmarks → favorites_count, total_comments → comments_count.
  - The user_id defaults to the authenticated account (from the token response);
    an explicit target user_id can be supplied to track someone else's public works.
  - Pagination: Pixiv returns a `next_url` (offset-based) per page.
"""

from __future__ import annotations
import asyncio
import hashlib
import logging
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse, parse_qs

import httpx

import config

logger = logging.getLogger(__name__)

# Well-known Pixiv mobile-app OAuth constants (same as pixivpy / public clients).
_AUTH_URL = "https://oauth.secure.pixiv.net/auth/token"
_API_BASE = "https://app-api.pixiv.net"
_CLIENT_ID = "MOBrBDS9blbrk2o0u85bMpRTQ2Tn"
_CLIENT_SECRET = "lsACyCD94FhDUtGTXi3QzcFE2uU1hqtDaKeqrdwj"
_HASH_SECRET = "28c1fdd170a5204386cb1313c7077b34f83e4aaf4aa829ce78c231e05b0bae2c"

_APP_HEADERS = {
    "User-Agent": "PixivIOSApp/7.13.3 (iOS 14.6; iPhone13,2)",
    "App-OS": "ios",
    "App-OS-Version": "14.6",
    "Accept-Language": "en-US",
}

_RATING_MAP = {0: "General", 1: "R-18", 2: "R-18G"}


def _safe_int(val: Any) -> int:
    if val is None:
        return 0
    try:
        if isinstance(val, str):
            val = val.replace(",", "").strip()
        return int(val)
    except (ValueError, TypeError):
        return 0


class PixClient:
    """Async client for Pixiv's reverse-engineered app-API."""

    def __init__(self, refresh_token: str = "", user_id: str = "",
                 proxy_url: str = "", proxy_key: str = ""):
        self.refresh_token = refresh_token
        self.user_id = str(user_id or "")     # target user; "" → authenticated self
        self._access_token: str = ""
        self._auth_user_id: str = ""          # the authenticated account's id
        self._username: str = ""
        self._logged_in = False

        if proxy_url and proxy_key:
            from polling.cf_proxy import CloudflareProxyTransport
            transport = CloudflareProxyTransport(proxy_url, proxy_key)
            logger.info("Pix client using CF proxy: %s", proxy_url)
        else:
            transport = httpx.AsyncHTTPTransport(retries=2)
        self._http = httpx.AsyncClient(
            timeout=30.0,
            follow_redirects=True,
            transport=transport,
        )

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        await self.close()

    async def close(self) -> None:
        await self._http.aclose()

    def update_credentials(self, refresh_token: str, user_id: str = "") -> None:
        new_uid = str(user_id or "")
        changed = (self.refresh_token != refresh_token or self.user_id != new_uid)
        self.refresh_token = refresh_token
        self.user_id = new_uid
        if changed:
            self._logged_in = False
            self._access_token = ""

    # -- Auth -----------------------------------------------------------------

    async def _refresh_access_token(self) -> bool:
        """Exchange the refresh token for a fresh access token."""
        if not self.refresh_token:
            return False
        client_time = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")
        client_hash = hashlib.md5((client_time + _HASH_SECRET).encode("utf-8")).hexdigest()
        headers = {
            **_APP_HEADERS,
            "X-Client-Time": client_time,
            "X-Client-Hash": client_hash,
            "Content-Type": "application/x-www-form-urlencoded",
        }
        data = {
            "client_id": _CLIENT_ID,
            "client_secret": _CLIENT_SECRET,
            "grant_type": "refresh_token",
            "include_policy": "true",
            "refresh_token": self.refresh_token,
        }
        try:
            resp = await self._http.post(_AUTH_URL, data=data, headers=headers)
            if resp.status_code != 200:
                logger.error("PIX: token refresh failed (%s): %s", resp.status_code, resp.text[:200])
                return False
            body = resp.json()
            # Pixiv has historically nested the payload under "response"; newer
            # responses are flat. Handle both.
            payload = body.get("response", body)
            self._access_token = payload.get("access_token", "")
            new_refresh = payload.get("refresh_token")
            if new_refresh:
                self.refresh_token = new_refresh   # rotate
            user = payload.get("user", {}) or {}
            self._auth_user_id = str(user.get("id", "")) or self._auth_user_id
            self._username = user.get("name") or user.get("account") or self._username
            return bool(self._access_token)
        except Exception as e:
            logger.error("PIX: token refresh error: %s", e)
            return False

    async def validate_session(self) -> str | None:
        """Refresh the access token. Returns the authenticated username on success."""
        if not await self._refresh_access_token():
            return None
        self._logged_in = True
        if not self.user_id:
            self.user_id = self._auth_user_id   # default to tracking self
        return self._username or self._auth_user_id or "pixiv"

    async def ensure_logged_in(self) -> bool:
        if self._logged_in and self._access_token:
            return True
        return bool(await self.validate_session())

    async def get_follower_count(self) -> int | None:
        """Best-effort follower count from /v1/user/detail.

        Pixiv's app-API user-detail `profile` object isn't guaranteed to carry a
        follower total, so probe the plausible keys and return None otherwise —
        the follower series just won't populate for Pixiv rather than storing a
        following-count in its place.
        """
        if not await self.ensure_logged_in():
            return None
        uid = self.user_id or self._auth_user_id
        if not uid:
            return None
        data = await self._get_json(f"{_API_BASE}/v1/user/detail", params={"user_id": uid})
        if not data or not isinstance(data, dict):
            return None
        profile = data.get("profile", {}) or {}
        for key in ("total_follower", "total_followers", "follower"):
            if profile.get(key) is not None:
                return _safe_int(profile.get(key))
        return None

    # -- HTTP Helpers ---------------------------------------------------------

    async def _get_json(self, url: str, params: dict | None = None, _retried: bool = False) -> dict | None:
        headers = {**_APP_HEADERS, "Authorization": f"Bearer {self._access_token}"}
        try:
            resp = await self._http.get(url, params=params, headers=headers)
            if resp.status_code in (400, 401) and not _retried:
                # Access token likely expired — refresh once and retry.
                logger.info("PIX: %s, refreshing access token...", resp.status_code)
                if await self._refresh_access_token():
                    return await self._get_json(url, params, _retried=True)
                return None
            if resp.status_code == 429:
                logger.warning("PIX: Rate limited (429), waiting 30s...")
                await asyncio.sleep(30)
                resp = await self._http.get(url, params=params, headers=headers)
            if resp.status_code == 404:
                logger.warning("PIX: Not found (404) for %s", url)
                return None
            resp.raise_for_status()
            return resp.json()
        except httpx.HTTPError as e:
            logger.error("PIX: Failed to fetch %s: %s", url, e)
            return None
        except Exception as e:
            logger.error("PIX: JSON parse error for %s: %s", url, e)
            return None

    # -- Work Discovery -------------------------------------------------------

    async def _fetch_works(self, kind: str) -> list[dict]:
        """Page through /v1/user/{illusts|novels} following next_url."""
        items: list[dict] = []
        key = "illusts" if kind == "illust" else "novels"
        url = f"{_API_BASE}/v1/user/{key}"
        params: dict | None = {"user_id": self.user_id}
        if kind == "illust":
            params["type"] = "illust"

        for _page_safety in range(2000):
            data = await self._get_json(url, params)
            if not data or not isinstance(data, dict):
                break
            works = data.get(key) or []
            for w in works:
                items.append({"work": w, "kind": kind})
            next_url = data.get("next_url")
            if not next_url or not works:
                break
            # next_url carries all params (offset etc.) — follow it directly.
            url, params = next_url, None
            await asyncio.sleep(config.PIX_REQUEST_DELAY_SECONDS)
        return items

    async def get_all_post_uris(self) -> list[dict]:
        """Fetch all illustrations + novels for the tracked user. Items carry the
        raw work (counts come with the listing — no second fetch)."""
        if not await self.ensure_logged_in():
            logger.error("PIX: Not logged in, cannot fetch works")
            return []

        all_items: list[dict] = []
        seen: set[str] = set()
        for kind in ("illust", "novel"):
            try:
                for item in await self._fetch_works(kind):
                    w = item["work"]
                    wid = str(w.get("id", ""))
                    uri = f"{kind}:{wid}"
                    if not wid or uri in seen:
                        continue
                    seen.add(uri)
                    all_items.append({"post_uri": uri, "work": w, "kind": kind})
            except Exception as e:
                logger.warning("PIX: failed to fetch %ss: %s", kind, e)

        logger.info("PIX: Found %d works for user %s", len(all_items), self.user_id)
        return all_items

    # -- Work Details ---------------------------------------------------------

    async def get_post_details_batch(self, items: list[dict]) -> list[dict]:
        """Parse the raw works gathered in discovery — no extra API calls."""
        details: list[dict] = []
        for item in items:
            work = item.get("work")
            detail = (self._parse_work(work, item.get("kind", "illust"))
                      if work else self._empty_detail(item.get("post_uri", "")))
            details.append(detail)
        return details

    # -- Parsing Helpers ------------------------------------------------------

    def _parse_work(self, w: dict, kind: str) -> dict:
        wid = str(w.get("id", ""))
        uri = f"{kind}:{wid}"
        user = w.get("user", {}) or {}

        # Thumbnail: illusts expose image_urls; novels expose image_urls too.
        img = w.get("image_urls", {}) or {}
        thumbnail_url = img.get("medium") or img.get("square_medium") or img.get("large") or ""

        if kind == "novel":
            content_type = "novel"
            link = f"https://www.pixiv.net/novel/show.php?id={wid}"
        else:
            content_type = w.get("type", "illust")   # illust / manga / ugoira
            link = f"https://www.pixiv.net/artworks/{wid}"

        tags = []
        for t in (w.get("tags") or []):
            name = t.get("name") if isinstance(t, dict) else None
            if name:
                tags.append(name)

        caption = w.get("caption", "") or ""

        return {
            "post_uri": uri,
            "title": w.get("title", "") or "(untitled)",
            "full_text": caption,
            "username": user.get("name") or user.get("account", self._username),
            "posted_at": w.get("create_date", ""),
            "content_type": content_type,
            "rating": _RATING_MAP.get(_safe_int(w.get("x_restrict", 0)), "General"),
            "description": caption,
            "keywords": tags,
            "link": link,
            "thumbnail_url": thumbnail_url,
            "views": _safe_int(w.get("total_view", 0)),
            "favorites_count": _safe_int(w.get("total_bookmarks", 0)),
            "comments_count": _safe_int(w.get("total_comments", 0)),
            "has_media": 1,
            "embed_type": content_type,
        }

    def _empty_detail(self, uri: str) -> dict:
        return {
            "post_uri": uri,
            "title": "",
            "full_text": "",
            "username": self._username,
            "posted_at": "",
            "content_type": "illust",
            "rating": "General",
            "description": "",
            "keywords": [],
            "link": "",
            "thumbnail_url": "",
            "views": 0,
            "favorites_count": 0,
            "comments_count": 0,
            "has_media": 0,
            "embed_type": "",
        }
