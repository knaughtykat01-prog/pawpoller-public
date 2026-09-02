"""Official X (Twitter) API v2 backend for the X poll path.

Opt-in, bring-your-own-token. When the user configures an X API **Bearer token**
(``tw_api_bearer_token``, stored in the credential vault), the poll path prefers
this official, ToS-compliant, **IP-agnostic** backend over the gallery-dl /
GraphQL scrapers — it authenticates per-token, so it is not subject to the
per-datacenter-IP rate limiting that throttles the scrapers on a server.

Scope: READ ONLY, and only the tracked account's own tweets. It reads
``public_metrics`` — the exact six metrics PawPoller already stores
(impression→views, like, retweet, reply, quote, bookmark) — so nothing in the
schema/poller/routes changes. Posting stays entirely on the GraphQL client
(the official write API costs $0.015+/post and we have a working free path).

Cost note (pay-per-use): as of 2026 X has no free tier; owned-account reads are
~$0.001 each. We only read on the normal poll cadence — callers must NOT
force_full on a timer. One dev app / Bearer token covers ALL of a user's
accounts (public_metrics reads any public account). See
docs/specs/x_official_api.md.

Priority in the hybrid (clients/tw/client.py):
    official (this) -> gallery-dl -> GraphQL scrape
Each backend returns None when it is not its turn / unavailable / errored, so
the caller simply falls through. This backend never posts and never touches
cookies.
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any

import httpx

import config

logger = logging.getLogger(__name__)

_API_BASE = "https://api.x.com/2"

# X usernames are 1-15 chars of [A-Za-z0-9_]. We interpolate the handle into the
# request path, so validate it first — a bad value can't path-traverse to another
# X endpoint (defence-in-depth; same host + own token, but no reason to allow it).
_HANDLE_RE = re.compile(r"^[A-Za-z0-9_]{1,15}$")

# X caps the user-timeline lookback at ~3,200 tweets → 32 pages of 100.
_MAX_PAGES = 32
_PAGE_SIZE = 100

# Follower count captured during a fetch, keyed by lowercased handle, so
# get_follower_count() in the same poll cycle doesn't spend a second billed
# user-lookup call. Warmed by fetch_tweets()/validate()/get_follower_count().
_LAST_FOLLOWERS: dict[str, int] = {}

_IMAGE_TYPES = {"photo"}


# -- Discovery / enable ------------------------------------------------------

def get_bearer_token(settings: dict | None = None) -> str:
    settings = settings if settings is not None else config.get_settings()
    return (settings.get("tw_api_bearer_token") or "").strip()


def is_enabled(settings: dict | None = None) -> bool:
    """Whether the official-API backend should be attempted.

    ``tw_polling_backend``: ``"auto"`` (default) or ``"official"`` use the
    official API when a Bearer token is present; ``"graphql"`` and ``"gallerydl"``
    explicitly select a scraper, so they disable this backend.
    """
    settings = settings if settings is not None else config.get_settings()
    backend = (settings.get("tw_polling_backend") or "auto").strip().lower()
    if backend in ("graphql", "gallerydl"):
        return False
    return bool(get_bearer_token(settings))


# -- Small helpers -----------------------------------------------------------

def _safe_int(val: Any) -> int:
    if val is None:
        return 0
    try:
        if isinstance(val, str):
            val = val.replace(",", "").strip()
        return int(val)
    except (ValueError, TypeError):
        return 0


def _normalize_date(val: Any) -> str:
    """X API v2 `created_at` is ISO 8601 ('2023-09-01T12:00:00.000Z'). Normalise
    to 'YYYY-MM-DD HH:MM:SS' (UTC); '' if unparseable."""
    if not val or not isinstance(val, str):
        return ""
    s = val.strip().replace("T", " ")
    s = s.split(".")[0].split("+")[0].replace("Z", "").strip()
    return s[:19]


def _auth_headers(bearer: str) -> dict:
    """Headers for an explicit Bearer token — passed in, never read from global
    settings, so the connect/validate flow works before the token is saved."""
    return {
        "Authorization": f"Bearer {bearer}",
        "User-Agent": f"PawPoller/{config.APP_VERSION}",
        "Accept": "application/json",
    }


# -- HTTP: user resolution ---------------------------------------------------

async def _resolve_user(http: httpx.AsyncClient, handle: str) -> tuple[str | None, int | None, int]:
    """Resolve @handle → (user_id, followers_count, http_status).

    Returns (None, None, status) on any non-200. followers_count comes from the
    same call (user.fields=public_metrics), so no extra billed request is needed.
    """
    if not _HANDLE_RE.match(handle or ""):
        logger.warning("TW official API: invalid X handle %r — skipping", handle)
        return None, None, 0
    from polling.rate_limit import tw_acquire
    try:
        await tw_acquire()
        r = await http.get(f"/users/by/username/{handle}",
                            params={"user.fields": "public_metrics"})
    except httpx.HTTPError as e:
        logger.warning("TW official API: user lookup failed: %s", e)
        return None, None, 0
    if r.status_code != 200:
        # 401/403 = bad/insufficient token; surfaced to the caller for validate().
        logger.warning("TW official API: user lookup %s for @%s: %s",
                       r.status_code, handle, r.text[:200])
        return None, None, r.status_code
    data = (r.json() or {}).get("data") or {}
    uid = data.get("id")
    followers = None
    pm = data.get("public_metrics") or {}
    if "followers_count" in pm:
        followers = _safe_int(pm.get("followers_count"))
    return (str(uid) if uid else None), followers, 200


# -- Parsing -----------------------------------------------------------------

def _build_detail(tweet: dict, handle: str, media_by_key: dict) -> dict:
    """One X API v2 tweet object → TWClient's detail-dict shape (identical keys
    to clients.tw.client.TWClient._extract_tweet_stats)."""
    tid = str(tweet.get("id"))
    text = tweet.get("text") or ""
    pm = tweet.get("public_metrics") or {}

    # Content type from referenced_tweets (retweeted / quoted / replied_to).
    refs = tweet.get("referenced_tweets") or []
    ref_types = {r.get("type") for r in refs if isinstance(r, dict)}
    if "retweeted" in ref_types:
        content_type = "retweet"
    elif "quoted" in ref_types:
        content_type = "quote"
    elif "replied_to" in ref_types:
        content_type = "reply"
    else:
        content_type = "tweet"

    hashtags = ((tweet.get("entities") or {}).get("hashtags")) or []
    keywords = [h.get("tag") for h in hashtags if isinstance(h, dict) and h.get("tag")]

    # Photos only (importable); videos/GIFs give a preview, not an image.
    media_urls: list[str] = []
    for key in ((tweet.get("attachments") or {}).get("media_keys") or []):
        m = media_by_key.get(key) or {}
        if m.get("type") in _IMAGE_TYPES:
            url = m.get("url") or ""
            if url and url not in media_urls:
                media_urls.append(url)

    return {
        "tweet_id": tid,
        "title": (text[:80] + "...") if len(text) > 80 else text,
        "username": handle,
        "posted_at": _normalize_date(tweet.get("created_at")),
        "content_type": content_type,
        "rating": "General",
        "description": text,
        "keywords": keywords,
        "link": f"https://x.com/{handle}/status/{tid}",
        "thumbnail_url": media_urls[0] if media_urls else "",
        "media_urls": media_urls,
        "views": _safe_int(pm.get("impression_count")),
        "likes": _safe_int(pm.get("like_count")),
        "retweets": _safe_int(pm.get("retweet_count")),
        "replies": _safe_int(pm.get("reply_count")),
        "quotes": _safe_int(pm.get("quote_count")),
        "bookmarks": _safe_int(pm.get("bookmark_count")),
    }


def _parse_page(body: dict, handle: str, media_by_key: dict) -> list[dict]:
    """Merge a page's `includes.media` into media_by_key and map its tweets."""
    for m in ((body.get("includes") or {}).get("media") or []):
        if m.get("media_key"):
            media_by_key[m["media_key"]] = m
    return [_build_detail(t, handle, media_by_key) for t in (body.get("data") or [])]


# -- Public API --------------------------------------------------------------

_TWEET_FIELDS = "public_metrics,created_at,entities,referenced_tweets,attachments"
_EXPANSIONS = "attachments.media_keys"
_MEDIA_FIELDS = "url,type,preview_image_url"


async def fetch_tweets(bearer: str | None, handle: str,
                       settings: dict | None = None) -> list[dict] | None:
    """Fetch the tracked account's own tweets via the official X API v2.

    Returns a list of detail dicts (authoritative — even an empty list means the
    account genuinely has no matching tweets), or ``None`` when the backend is
    disabled/unconfigured or the *first* request failed (so the caller falls back
    to gallery-dl → GraphQL). A later-page failure returns the partial data
    already collected rather than discarding it.
    """
    settings = settings if settings is not None else config.get_settings()
    if not is_enabled(settings):
        return None
    bearer = (bearer or get_bearer_token(settings)).strip()
    handle = (handle or "").lstrip("@")
    if not (bearer and handle):
        return None

    try:
        async with httpx.AsyncClient(base_url=_API_BASE, headers=_auth_headers(bearer),
                                     timeout=30.0) as http:
            uid, followers, _status = await _resolve_user(http, handle)
            if uid is None:
                return None  # bad token / handle → fall back to a scraper
            if followers is not None:
                _LAST_FOLLOWERS[handle.lower()] = followers

            tweets: list[dict] = []
            media_by_key: dict = {}
            token: str | None = None
            for page in range(_MAX_PAGES):
                params = {
                    "max_results": _PAGE_SIZE,
                    "exclude": "retweets",  # own posts; a pure RT's metrics belong to the original author
                    "tweet.fields": _TWEET_FIELDS,
                    "expansions": _EXPANSIONS,
                    "media.fields": _MEDIA_FIELDS,
                }
                if token:
                    params["pagination_token"] = token
                from polling.rate_limit import tw_acquire
                await tw_acquire()
                r = await http.get(f"/users/{uid}/tweets", params=params)
                if r.status_code != 200:
                    logger.warning("TW official API: tweets page %d → %s: %s",
                                   page, r.status_code, r.text[:200])
                    # First page failed with nothing collected → fall back.
                    if page == 0 and not tweets:
                        return None
                    break  # later-page failure → keep the partial data
                body = r.json() or {}
                tweets.extend(_parse_page(body, handle, media_by_key))
                token = (body.get("meta") or {}).get("next_token")
                if not token:
                    break
                await asyncio.sleep(0.3)  # gentle pacing between pages

            logger.info("TW official API: %d tweets for @%s", len(tweets), handle)
            return tweets
    except httpx.HTTPError as e:
        logger.warning("TW official API: fetch failed — falling back: %s", e)
        return None


async def validate(bearer: str | None, handle: str,
                   settings: dict | None = None) -> bool | None:
    """Validate the Bearer token via a single user-lookup.

    ``True``/``False`` when definitive (200 vs 401/403), or ``None`` when the
    backend is disabled or the failure was ambiguous (network/5xx) — caller
    should then fall back to scraper validation.
    """
    settings = settings if settings is not None else config.get_settings()
    if not is_enabled(settings):
        return None
    bearer = (bearer or get_bearer_token(settings)).strip()
    handle = (handle or "").lstrip("@")
    if not (bearer and handle):
        return None
    try:
        async with httpx.AsyncClient(base_url=_API_BASE, headers=_auth_headers(bearer),
                                     timeout=30.0) as http:
            uid, followers, status = await _resolve_user(http, handle)
            if uid:
                if followers is not None:
                    _LAST_FOLLOWERS[handle.lower()] = followers
                return True
            if status in (401, 403):
                return False  # token is bad / lacks access
            return None  # ambiguous → fall back
    except httpx.HTTPError:
        return None


async def get_follower_count(bearer: str | None, handle: str,
                             settings: dict | None = None) -> int | None:
    """Follower count for the tracked account, or ``None``.

    Prefers the value cached during this cycle's fetch_tweets() (no extra billed
    call); otherwise makes one user-lookup. Best-effort — never raises.
    """
    settings = settings if settings is not None else config.get_settings()
    if not is_enabled(settings):
        return None
    h = (handle or "").lstrip("@")
    if not h:
        return None
    cached = _LAST_FOLLOWERS.get(h.lower())
    if cached is not None:
        return cached
    bearer = (bearer or get_bearer_token(settings)).strip()
    if not bearer:
        return None
    try:
        async with httpx.AsyncClient(base_url=_API_BASE, headers=_auth_headers(bearer),
                                     timeout=30.0) as http:
            _uid, followers, _status = await _resolve_user(http, h)
            if followers is not None:
                _LAST_FOLLOWERS[h.lower()] = followers
            return followers
    except httpx.HTTPError:
        return None
