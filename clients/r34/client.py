"""Rule34.xxx API client: track an account's uploads, artist tag and characters (spec 028 US6).

Tracking only. Rule34.xxx gives apps no way to upload, and its website has bot checks, so PawPoller never
posts there (constitution VII). Reads go to the API host, which answers JSON without the website's checks:

  GET https://api.rule34.xxx/index.php?page=dapi&s=post&q=index&json=1&tags=<tags>&pid=<page>&limit=<=1000
      &api_key=<key>&user_id=<id>

Since August 2025 every request needs the account's own **API key and user id** (Rule34 → My Account →
Options → API Access Credentials). Without them the body is the JSON string "Missing authentication…".

Posts carry ``score`` and ``comment_count``; Rule34 shares no views and no favourite count. ``creator_id``
(or ``owner``, the uploader's name, depending on the response) marks the account's own uploads: the
``user_id`` credential IS the account's id.

Each tracked tag is its own search, merged by post id (Rule34's OR syntax isn't relied on). Deep pages can
come back unparseable; then the search restarts at page 0 with ``id:<<lowest id read>`` added, the way
gallery-dl (which PawPoller already ships) reads Rule34, so a bad page never ends the check early.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx

import config

logger = logging.getLogger(__name__)

API = "https://api.rule34.xxx/index.php"
SITE = "https://rule34.xxx"
PER_PAGE = 1000
REQUEST_DELAY = 1.0           # at most one request a second
HTTP_TIMEOUT = 30.0
_RATING = {"explicit": "adult", "e": "adult", "questionable": "mature", "q": "mature",
           "safe": "general", "s": "general", "general": "general", "sensitive": "mature"}
_ANIM = {"gif": "animation", "webm": "video", "mp4": "video"}


class Rule34AuthError(RuntimeError):
    """Rule34 refused the key and user id."""


def _safe_int(v: Any) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return 0


class Rule34Client:
    def __init__(self, username: str = "", api_key: str = "", user_id: str = ""):
        self.username = (username or "").strip()
        self.api_key = (api_key or "").strip()
        self.user_id = str(user_id or "").strip()
        self._client: httpx.AsyncClient | None = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        await self.close()

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    def update_credentials(self, username: str, api_key: str, user_id: str = "") -> None:
        self.username, self.api_key, self.user_id = (username or "").strip(), (api_key or "").strip(), \
            str(user_id or "").strip()

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=HTTP_TIMEOUT, headers={
                "User-Agent": f"PawPoller/{config.APP_VERSION} (rule34 self-analytics; user {self.user_id or 'unset'})",
                "Accept": "application/json"})
        return self._client

    async def _posts(self, tags: str, pid: int) -> list[dict] | None:
        """One page of posts; [] at the end; None when the page couldn't be read."""
        params = {"page": "dapi", "s": "post", "q": "index", "json": 1, "tags": tags, "pid": pid,
                  "limit": PER_PAGE, "api_key": self.api_key, "user_id": self.user_id}
        try:
            r = await self._http().get(API, params=params)
        except httpx.HTTPError as e:
            logger.warning("rule34 request failed: %s", e)
            return None
        text = (r.text or "").strip()
        if not text:
            return []
        try:
            data = r.json()
        except ValueError:
            return None
        if isinstance(data, str):       # "Missing authentication…" and other refusals
            if "auth" in data.lower():
                raise Rule34AuthError(data)
            return None
        return data if isinstance(data, list) else None

    async def validate_session(self) -> str | None:
        """The key and id work → the username (or the id when no name was given)."""
        if not (self.api_key and self.user_id):
            return None
        try:
            page = await self._posts("", 0)
        except Rule34AuthError:
            return None
        return (self.username or self.user_id) if page is not None else None

    async def ensure_logged_in(self) -> bool:
        return bool(await self.validate_session())

    async def search(self, tags: str, known: set[str] | None = None, seen: set[str] | None = None) -> list[dict]:
        """Every post for one tag search, newest first, paging with an ``id:<`` cursor after a bad page."""
        seen = seen if seen is not None else set()
        out: list[dict] = []
        pid, cursor, retried = 0, None, 0
        for _safety in range(200):
            q = f"{tags} id:<{cursor}".strip() if cursor else tags
            page = await self._posts(q, pid)
            if page is None:
                if not out or retried >= 3:
                    break
                cursor = min(_safe_int(p["id"]) for p in out)   # restart below the lowest id read
                pid, retried = 0, retried + 1
                await asyncio.sleep(REQUEST_DELAY)
                continue
            if not page:
                break
            ids = []
            for p in page:
                pid_s = str(_safe_int(p.get("id")))
                ids.append(pid_s)
                if pid_s != "0" and pid_s not in seen:
                    seen.add(pid_s)
                    out.append(p)
            if len(page) < PER_PAGE or (known is not None and all(i in known for i in ids)):
                break
            pid += 1
            await asyncio.sleep(REQUEST_DELAY)
        return out

    async def get_all_post_uris(self, queries: list[str] | None = None,
                                known: set[str] | None = None) -> list[dict]:
        """The account's uploads plus each tracked tag, one search each, merged by post id."""
        seen: set[str] = set()
        items: list[dict] = []
        first = [f"user:{self.username}"] if self.username else []
        for q in first + list(queries or []):
            for p in await self.search(q, known, seen):
                items.append({"post_uri": str(_safe_int(p.get("id"))), "raw": p})
            await asyncio.sleep(REQUEST_DELAY)
        logger.info("rule34: found %d posts", len(items))
        return items

    async def get_post_details_batch(self, items: list[dict]) -> list[dict]:
        return [self._parse(it.get("raw") or {}) for it in items]

    async def tag_count(self, tag: str) -> int | None:
        """Posts carrying a tag (None when none do: Rule34 has no tag lookup in its JSON API)."""
        page = await self._posts(tag, 0)
        return len(page) if page else None

    def _parse(self, p: dict) -> dict:
        pid = str(_safe_int(p.get("id")))
        tags = str(p.get("tags") or "").split()
        file_url = p.get("file_url") or ""
        ext = file_url.rsplit(".", 1)[-1].lower() if "." in file_url else ""
        creator = str(p.get("creator_id") or "")
        owner = str(p.get("owner") or "")
        return {
            "post_uri": pid,
            "title": f"#{pid}",
            "full_text": "",
            "username": self.username,
            "uploader_id": creator,
            "uploader_name": owner,
            "posted_at": p.get("created_at") or "",
            "content_type": _ANIM.get(ext, "image"),
            "rating": _RATING.get(str(p.get("rating") or "").lower(), ""),
            "description": "",
            "keywords": tags,
            "link": f"{SITE}/index.php?page=post&s=view&id={pid}",
            "thumbnail_url": p.get("preview_url") or p.get("sample_url") or "",
            "file_url": file_url,
            "score": _safe_int(p.get("score")),
            "up_score": 0,
            "down_score": 0,
            "favorites_count": 0,
            "comments_count": _safe_int(p.get("comment_count")),
            "has_media": 1 if file_url else 0,
        }
