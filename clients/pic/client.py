"""Picarto (pic) public API client — channel analytics (spec 013, 4.46.0).

Picarto is an art livestreaming site. Its public REST API v1 answers channel reads
with no login at all, so the only credential is the channel name:

  Channel:    GET {base}/channel/name/{name}          → views, followers, subscribers, live state
  Recordings: GET {base}/channel/name/{name}/videos   → past streams (a Picarto premium feature)

Docs: the Swagger at https://api.picarto.tv/ (v1.2.6). 30 requests/minute; a poll uses two.

Quirks, all observed live (specs/013-picarto-analytics/research.md):
  * ``duration`` on a recording is MILLISECONDS, though the docs say seconds.
  * ``views`` on a recording is 0 every time — not used.
  * Adult channels are hidden from ``/online`` and search unless asked, so this
    client only ever uses the by-name endpoints, which return them like any other.
  * A missing channel is a 404 with the body ``"Channel does not exist"``; some bad
    inputs give a 5xx instead. Both mean "no such channel" here.
"""
from __future__ import annotations

import logging
import re
from typing import Any

import httpx

logger = logging.getLogger(__name__)

API_BASE = "https://api.picarto.tv/api/v1"
SITE = "https://picarto.tv"
HTTP_TIMEOUT = 20.0
USER_AGENT = "PawPoller (picarto channel analytics)"

# Picarto channel names: letters, digits, underscore, dash. Anything else can't be one.
_NAME = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


class PicError(Exception):
    """Picarto answered, but not with what we asked for (rate limit, outage)."""


def clean_channel(raw: str) -> str:
    """A channel name from what a user typed — tolerates a pasted picarto.tv address or a leading @."""
    s = (raw or "").strip().rstrip("/")
    if "picarto.tv/" in s:
        s = s.split("picarto.tv/", 1)[1].split("/", 1)[0].split("?", 1)[0]
    s = s.lstrip("@")
    return s if _NAME.match(s) else ""


def _int(v: Any) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return 0


def parse_channel(d: dict) -> dict:
    """The fields PawPoller keeps from a channel record."""
    return {
        "user_id": _int(d.get("user_id")),
        "name": str(d.get("name") or ""),
        "views": _int(d.get("viewers_total")),
        "followers": _int(d.get("followers")),
        "subscribers": _int(d.get("subscribers")),
        "online": bool(d.get("online")),
        "viewers": _int(d.get("viewers")),
        "last_live": d.get("last_live") or None,
        "adult": bool(d.get("adult")),
        "recordings": bool(d.get("recordings")),
        "avatar": str(d.get("avatar") or ""),
        "title": str(d.get("title") or ""),
    }


def parse_video(v: dict, channel: str) -> dict:
    """One recording → a pic_submissions row. Duration stays in milliseconds (see module notes)."""
    vid = str(v.get("id") or "")
    thumbs = v.get("thumbnails") if isinstance(v.get("thumbnails"), dict) else {}
    ts = str(v.get("timestamp") or "")
    # "2023-11-23T08:33:21.000000Z" → "2023-11-23 08:33:21" (the shape every other table stores)
    posted = ts.replace("T", " ")[:19] if ts else None
    return {
        "submission_id": vid,
        "title": str(v.get("title") or v.get("stream_name") or "Untitled stream"),
        "username": channel,
        "link": f"{SITE}/{channel}/profile/videos/{vid}" if vid else "",
        "thumbnail_url": str(thumbs.get("web_large") or thumbs.get("web") or ""),
        "duration_ms": _int(v.get("duration")),
        "adult": 1 if v.get("adult") else 0,
        "posted_at": posted,
    }


class PicClient:
    def __init__(self, channel: str = "", base_url: str = API_BASE):
        self.channel = clean_channel(channel)
        self.base_url = base_url.rstrip("/")
        self._client: httpx.AsyncClient | None = None
        self._channel_cache: dict | None = None

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=HTTP_TIMEOUT, headers={"User-Agent": USER_AGENT})
        return self._client

    async def _get(self, path: str) -> Any:
        r = await self._http().get(f"{self.base_url}{path}")
        if r.status_code == 429:
            raise PicError("Picarto is rate-limiting requests right now — the next poll will try again.")
        if r.status_code == 404 or r.status_code >= 500:
            return None
        if r.status_code != 200:
            raise PicError(f"Picarto answered HTTP {r.status_code}.")
        return r.json()

    async def get_channel(self, refresh: bool = False) -> dict | None:
        """The channel's public record, or None when there's no such channel. Cached per poll."""
        if not self.channel:
            return None
        if self._channel_cache is None or refresh:
            d = await self._get(f"/channel/name/{self.channel}")
            self._channel_cache = parse_channel(d) if isinstance(d, dict) and d.get("name") else None
        return self._channel_cache

    async def validate_session(self) -> str | None:
        """The channel name as Picarto spells it, or None. (Named like every client's login check.)"""
        ch = await self.get_channel(refresh=True)
        return ch["name"] if ch else None

    async def get_follower_count(self) -> int | None:
        """For polling/followers.capture_followers."""
        ch = await self.get_channel()
        return ch["followers"] if ch else None

    async def get_videos(self) -> list[dict]:
        """Recorded streams, parsed. [] when recording is off (a premium feature) or none exist."""
        if not self.channel:
            return []
        name = (self._channel_cache or {}).get("name") or self.channel
        d = await self._get(f"/channel/name/{name}/videos")
        if not isinstance(d, list):
            return []
        return [parse_video(v, name) for v in d if isinstance(v, dict) and v.get("id")]
