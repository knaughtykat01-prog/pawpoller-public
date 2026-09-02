"""Furbooru (Philomena) API client — poll a user's own uploads + stats.

Furbooru runs the **Philomena** booru engine (same as Derpibooru, Manebooru,
Ponybooru, Twibooru), which exposes a clean **public read JSON API** — no auth
needed to read public images. So polling a user's own uploads is a simple,
robust GET (unlike the cookie-scraping most boorus need). Because the client is
parameterised by ``base_url``, the same class serves every Philomena booru — a
future Derpibooru/Manebooru integration only sets a different base URL.

Metric shape matches e621 (booru model, no view count): **score** (upvotes −
downvotes, can be negative), up/down split, **faves**, **comments**. Rating is
derived from the safe/questionable/explicit tag Philomena requires on every image.

  List:      GET {base}/api/v1/json/search/images?q=uploaded_by:{user}&sf=id&sd=desc&per_page=50&page=N
  Per-image: GET {base}/api/v1/json/images/{id}
An optional API key (`&key=`) raises rate limits + lets you see your own hidden
images, but is not required for public uploads.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx

logger = logging.getLogger(__name__)

DEFAULT_BASE = "https://furbooru.org"
PER_PAGE = 50
HTTP_TIMEOUT = 30.0
REQUEST_DELAY = 1.0        # be polite to the booru
USER_AGENT = "PawPoller (furbooru self-analytics)"

# Philomena rating tags → our internal rating.
_RATING_TAGS = [("explicit", "adult"), ("questionable", "mature"),
                ("grimdark", "adult"), ("semi-grimdark", "mature"),
                ("suggestive", "mature"), ("safe", "general")]
_ANIM_FORMATS = {"gif": "animation", "webm": "video", "mp4": "video"}


def _safe_int(v: Any) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return 0


class FurbooruClient:
    def __init__(self, username: str = "", api_key: str = "", base_url: str = DEFAULT_BASE):
        self.username = (username or "").strip()
        self.api_key = (api_key or "").strip()
        self.base_url = (base_url or DEFAULT_BASE).rstrip("/")
        self._client: httpx.AsyncClient | None = None
        self._logged_in = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        await self.close()

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    def update_credentials(self, username: str, api_key: str) -> None:
        self.username = (username or "").strip()
        self.api_key = (api_key or "").strip()
        self._logged_in = False

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=HTTP_TIMEOUT, headers={"User-Agent": USER_AGENT})
        return self._client

    async def _get_json(self, path: str, params: dict | None = None) -> Any:
        params = dict(params or {})
        if self.api_key:
            params["key"] = self.api_key
        try:
            r = await self._http().get(f"{self.base_url}/{path.lstrip('/')}", params=params)
        except httpx.HTTPError as e:
            logger.warning("furbooru request failed: %s", e)
            return None
        if r.status_code >= 400:
            return None
        try:
            return r.json()
        except Exception:
            return None

    async def validate_session(self) -> str | None:
        """The public API needs no auth; 'valid' = the username resolves to a
        search that returns 200. Returns the username on success."""
        if not self.username:
            return None
        data = await self._get_json("/api/v1/json/search/images",
                                    {"q": f"uploaded_by:{self.username}", "per_page": 1})
        if data is None or "images" not in data:
            return None
        self._logged_in = True
        return self.username

    async def ensure_logged_in(self) -> bool:
        return self._logged_in or bool(await self.validate_session())

    async def get_all_post_uris(self) -> list[dict]:
        """Page the user's own uploads (newest first). Each listing carries full
        stats, so the raw image is stashed for get_post_details_batch()."""
        if not self.username:
            return []
        items: list[dict] = []
        seen: set[str] = set()
        for page in range(1, 400):          # 400*50 = 20k image ceiling
            data = await self._get_json("/api/v1/json/search/images", {
                "q": f"uploaded_by:{self.username}", "sf": "id", "sd": "desc",
                "per_page": PER_PAGE, "page": page})
            images = (data or {}).get("images") or []
            if not images:
                break
            for img in images:
                iid = str(_safe_int(img.get("id")))
                if not iid or iid in seen:
                    continue
                seen.add(iid)
                items.append({"post_uri": iid, "raw": img})
            if len(images) < PER_PAGE:
                break
            await asyncio.sleep(REQUEST_DELAY)
        logger.info("furbooru: found %d images for user %s", len(items), self.username)
        return items

    async def get_post_details_batch(self, items: list[dict]) -> list[dict]:
        return [self._parse_image(it.get("raw") or {}) for it in items]

    def _parse_image(self, img: dict) -> dict:
        iid = str(_safe_int(img.get("id")))
        tags = [str(t) for t in (img.get("tags") or [])]
        reps = img.get("representations") or {}
        thumb = reps.get("thumb") or reps.get("small") or reps.get("medium") or ""
        file_url = img.get("view_url") or reps.get("full") or ""
        fmt = (img.get("format") or "").lower()
        rating = ""
        low = {t.lower() for t in tags}
        for tag, r in _RATING_TAGS:
            if tag in low:
                rating = r
                break
        description = img.get("description", "") or ""
        first_line = description.strip().splitlines()[0].strip() if description.strip() else ""
        return {
            "post_uri": iid,
            "title": (first_line[:80] if first_line else f"#{iid}"),
            "full_text": description,
            "username": self.username,
            "posted_at": img.get("created_at", "") or "",
            "content_type": _ANIM_FORMATS.get(fmt, "image"),
            "rating": rating,
            "description": description,
            "keywords": tags,
            "link": f"{self.base_url}/images/{iid}",
            "thumbnail_url": thumb,
            "file_url": file_url,
            "score": _safe_int(img.get("score")),
            "up_score": _safe_int(img.get("upvotes")),
            "down_score": _safe_int(img.get("downvotes")),
            "favorites_count": _safe_int(img.get("faves")),
            "comments_count": _safe_int(img.get("comment_count")),
            "has_media": 1 if file_url else 0,
        }
