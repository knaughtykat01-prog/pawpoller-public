"""Wattpad (WP) API client.

Wattpad provides a public JSON API at api.wattpad.com. No authentication
is required — only the target username is needed. Stories are discovered
via the user profile endpoint and stats are fetched per-story.

Key details:
  - Story IDs are integers
  - Stats: readCount (reads), voteCount (votes), commentCount (comments), numParts,
    and the story appears in reading lists (num_lists)
  - Auth: none required (public API)
  - Rate limiting: moderate, 1s delay between requests
"""

from __future__ import annotations
import asyncio
import logging

import httpx

import config

logger = logging.getLogger(__name__)
_API_BASE = "https://api.wattpad.com"
_WEB_BASE = "https://www.wattpad.com"

_HEADERS = {
    "User-Agent": "PawPoller/1.0",
    "Accept": "application/json",
}


class WPClient:
    """Async HTTP client for Wattpad's public API."""

    def __init__(self, target_user: str, proxy_url: str = "", proxy_key: str = ""):
        self.target_user = target_user
        if proxy_url and proxy_key:
            from polling.cf_proxy import CloudflareProxyTransport
            transport = CloudflareProxyTransport(proxy_url, proxy_key)
            logger.info("WP client using CF proxy: %s", proxy_url)
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

    def update_credentials(self, target_user: str) -> None:
        self.target_user = target_user

    async def close(self) -> None:
        await self._http.aclose()

    async def _get_json(self, url: str, params: dict | None = None) -> dict | list | None:
        """Fetch a JSON endpoint with error handling."""
        try:
            resp = await self._http.get(url, params=params)
            if resp.status_code == 404:
                logger.warning("WP: Not found (404) for %s", url)
                return None
            if resp.status_code == 429:
                logger.warning("WP: Rate limited (429), waiting 30s...")
                await asyncio.sleep(30)
                resp = await self._http.get(url, params=params)
            resp.raise_for_status()
            return resp.json()
        except httpx.HTTPError as e:
            logger.error("WP: Failed to fetch %s: %s", url, e)
            return None
        except Exception as e:
            logger.error("WP: JSON parse error for %s: %s", url, e)
            return None

    # -- Validation --------------------------------------------------------

    async def validate_user(self) -> str | None:
        """Check if the target user exists on Wattpad. Returns username if valid."""
        if not self.target_user:
            return None
        data = await self._get_json(f"{_API_BASE}/api/v3/users/{self.target_user}")
        if data and data.get("username"):
            return data["username"]
        return None

    async def get_follower_count(self) -> int | None:
        """Return the tracked user's follower count (numFollowers)."""
        if not self.target_user:
            return None
        data = await self._get_json(
            f"{_API_BASE}/api/v3/users/{self.target_user}",
            params={"fields": "username,numFollowers"},
        )
        if data and isinstance(data, dict) and data.get("numFollowers") is not None:
            try:
                return int(data["numFollowers"])
            except (TypeError, ValueError):
                return None
        return None

    # -- Story Discovery ---------------------------------------------------

    async def get_all_story_ids(self) -> list[dict]:
        """Fetch all stories for the target user.

        Wattpad's API is split by endpoint: the per-user published-stories list
        lives on **v4**, while the old v3 path
        (``api/v3/users/{u}/stories/published``) now returns 400
        ``InvalidEndpoint``. So we call v4 first (the happy path logs nothing)
        and only fall back to v3 if v4 ever goes away.
        """
        all_stories: list[dict] = []
        offset = 0
        limit = 50

        for _page_safety in range(1000):
            data = await self._get_json(
                f"{_API_BASE}/v4/users/{self.target_user}/stories/published",
                params={"offset": str(offset), "limit": str(limit)},
            )

            if not data:
                # Legacy fallback, in case Wattpad flips the v3/v4 split again.
                data = await self._get_json(
                    f"{_API_BASE}/api/v3/users/{self.target_user}/stories/published",
                    params={"offset": str(offset), "limit": str(limit), "fields": "stories(id,title)"},
                )

            if not data:
                break

            stories = data.get("stories", data) if isinstance(data, dict) else data
            if not isinstance(stories, list):
                stories = data.get("stories", []) if isinstance(data, dict) else []

            if not stories:
                break

            for story in stories:
                story_id = story.get("id")
                if story_id:
                    all_stories.append({
                        "story_id": int(story_id),
                        "title": story.get("title", ""),
                    })

            if len(stories) < limit:
                break

            offset += limit
            await asyncio.sleep(config.WP_REQUEST_DELAY_SECONDS)

        logger.info("WP: Found %d stories for %s", len(all_stories), self.target_user)
        return all_stories

    # -- Story Details -----------------------------------------------------

    async def get_story_detail(self, story_id: int) -> dict:
        """Fetch full details for a single story."""
        data = await self._get_json(
            f"{_API_BASE}/api/v3/stories/{story_id}",
            params={"fields": "id,title,description,tags,user,createDate,modifyDate,voteCount,readCount,commentCount,numParts,completed,mature,url,cover,categories,length,numLists"},
        )

        if not data:
            # Try v4 endpoint
            data = await self._get_json(f"{_API_BASE}/v4/stories/{story_id}")

        if not data:
            return {
                "story_id": story_id, "title": "", "username": self.target_user,
                "reads": 0, "votes": 0, "comments_count": 0, "num_lists": 0,
                "keywords": [], "link": f"{_WEB_BASE}/story/{story_id}",
                "description": "", "category": "", "rating": "",
                "word_count": 0, "num_parts": 0, "completed": False,
                "posted_at": "",
            }

        detail = {
            "story_id": int(data.get("id", story_id)),
            "title": data.get("title", ""),
            "username": data.get("user", {}).get("name", self.target_user) if isinstance(data.get("user"), dict) else self.target_user,
            "description": data.get("description", ""),
            "reads": data.get("readCount", 0),
            "votes": data.get("voteCount", 0),
            "comments_count": data.get("commentCount", 0),
            "num_lists": data.get("numLists", 0),
            "word_count": data.get("length", 0),
            "num_parts": data.get("numParts", 0),
            "completed": bool(data.get("completed", False)),
            "rating": "Mature" if data.get("mature") else "General",
            "link": data.get("url", f"{_WEB_BASE}/story/{story_id}"),
            "posted_at": data.get("createDate", ""),
            "modified_at": data.get("modifyDate", ""),
        }

        # Tags/keywords
        tags = data.get("tags", [])
        if isinstance(tags, list):
            detail["keywords"] = [str(t) for t in tags]
        else:
            detail["keywords"] = []

        # Categories
        categories = data.get("categories", [])
        if isinstance(categories, list) and categories:
            detail["category"] = str(categories[0]) if categories else ""
        else:
            detail["category"] = ""

        # Cover image
        detail["cover_url"] = data.get("cover", "")

        return detail

    async def get_story_details_batch(self, story_ids: list[int]) -> list[dict]:
        """Fetch details for multiple stories with rate limiting."""
        details = []
        for i, story_id in enumerate(story_ids):
            if i > 0:
                await asyncio.sleep(config.WP_REQUEST_DELAY_SECONDS)
            try:
                detail = await self.get_story_detail(story_id)
                details.append(detail)
            except Exception as e:
                logger.warning("WP: Failed to fetch story %d: %s", story_id, e)
        return details
