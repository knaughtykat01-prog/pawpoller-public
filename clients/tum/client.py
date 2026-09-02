"""Tumblr (TUM) REST API client.

Read-only polling of a public blog needs only the app's **OAuth consumer
key** (Tumblr calls it the "API key") plus the blog identifier — no OAuth
token dance. Register an app at https://www.tumblr.com/oauth/apps and copy
"OAuth Consumer Key".

Key details:
  - Post IDs are numeric strings (id_string), stored as TEXT.
  - Engagement metric is **notes** (note_count = likes + reblogs + replies
    combined). Tumblr does NOT expose a reliable per-post breakdown — the
    notes array is truncated for popular posts — so we track the total only.
  - Posts are typed by Tumblr's own `type` (text / photo / quote / link /
    chat / audio / video / answer).
  - Pagination: offset/limit (limit max 20).
"""

from __future__ import annotations
import asyncio
import html
import logging
import re
from datetime import datetime, timezone
from typing import Any

import httpx

import config

logger = logging.getLogger(__name__)

_API_BASE = "https://api.tumblr.com/v2"

_HEADERS = {
    "User-Agent": "PawPoller/1.0",
    "Accept": "application/json",
}

_TAG_RE = re.compile(r"<[^>]+>")


def _safe_int(val: Any) -> int:
    if val is None:
        return 0
    try:
        if isinstance(val, str):
            val = val.replace(",", "").strip()
        return int(val)
    except (ValueError, TypeError):
        return 0


def _strip_html(body: str) -> str:
    if not body:
        return ""
    text = re.sub(r"<br\s*/?>|</p>", " ", body, flags=re.IGNORECASE)
    text = _TAG_RE.sub("", text)
    return html.unescape(text).strip()


def _normalise_blog(blog: str) -> str:
    """Normalise a blog identifier — strip whitespace, a leading @, and a URL
    wrapper. Tumblr accepts both ``name`` and ``name.tumblr.com``."""
    blog = (blog or "").strip().lstrip("@")
    if blog.startswith("http://") or blog.startswith("https://"):
        blog = blog.split("://", 1)[-1]
    return blog.rstrip("/")


# ── OAuth 1.0a signing (RFC 5849) ───────────────────────────────────
# Tumblr's read API takes just the consumer key, but CREATING a post needs a
# full OAuth1 user-token signature. No oauth library is installed, so this is a
# hand-rolled HMAC-SHA1 signer — unit-tested against Twitter's published example
# vector (tests/test_oauth1.py) so the crypto is verified without live tokens.

def _pe(s: Any) -> str:
    """Percent-encode per RFC 3986 (unreserved = ALPHA / DIGIT / -._~)."""
    from urllib.parse import quote
    return quote(str(s), safe="~")


def _oauth1_header(method: str, url: str, params: dict, *, consumer_key: str,
                   consumer_secret: str, token: str, token_secret: str,
                   timestamp: int, nonce: str) -> str:
    """Build an ``Authorization: OAuth ...`` header for a form-body request.

    ``params`` are the request's body/query params (for a form POST they're part
    of the signature base string, per RFC 5849 §3.4.1.3).
    """
    import base64
    import hashlib
    import hmac

    oauth = {
        "oauth_consumer_key": consumer_key,
        "oauth_nonce": nonce,
        "oauth_signature_method": "HMAC-SHA1",
        "oauth_timestamp": str(timestamp),
        "oauth_token": token,
        "oauth_version": "1.0",
    }
    all_params = {**params, **oauth}
    encoded = sorted((_pe(k), _pe(v)) for k, v in all_params.items())
    param_str = "&".join(f"{k}={v}" for k, v in encoded)
    base = "&".join([method.upper(), _pe(url), _pe(param_str)])
    signing_key = f"{_pe(consumer_secret)}&{_pe(token_secret)}"
    sig = base64.b64encode(
        hmac.new(signing_key.encode(), base.encode(), hashlib.sha1).digest()).decode()
    oauth["oauth_signature"] = sig
    return "OAuth " + ", ".join(f'{_pe(k)}="{_pe(v)}"' for k, v in sorted(oauth.items()))


class TumClient:
    """Async HTTP client for the Tumblr v2 API (read-only, API-key auth)."""

    def __init__(self, api_key: str = "", blog: str = "",
                 proxy_url: str = "", proxy_key: str = "",
                 consumer_secret: str = "", oauth_token: str = "",
                 oauth_token_secret: str = ""):
        self.api_key = api_key
        self.blog = _normalise_blog(blog)
        # OAuth1 user-token creds — only needed for POSTING (read uses api_key).
        self._consumer_secret = consumer_secret
        self._oauth_token = oauth_token
        self._oauth_token_secret = oauth_token_secret
        self._blog_name: str = ""
        self._logged_in = False

        if proxy_url and proxy_key:
            from polling.cf_proxy import CloudflareProxyTransport
            transport = CloudflareProxyTransport(proxy_url, proxy_key)
            logger.info("Tum client using CF proxy: %s", proxy_url)
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

    def update_credentials(self, api_key: str, blog: str) -> None:
        new_blog = _normalise_blog(blog)
        changed = (self.api_key != api_key or self.blog != new_blog)
        self.api_key = api_key
        self.blog = new_blog
        if changed:
            self._logged_in = False
            self._blog_name = ""

    # -- Auth -----------------------------------------------------------------

    async def validate_session(self) -> str | None:
        """Verify the API key + blog via /blog/{blog}/info. Returns the blog
        name on success."""
        if not self.api_key or not self.blog:
            return None
        data = await self._get_json(f"/blog/{self.blog}/info", {"api_key": self.api_key})
        blog = ((data or {}).get("response") or {}).get("blog") if isinstance(data, dict) else None
        if blog and blog.get("name"):
            self._blog_name = blog.get("name", self.blog)
            self._logged_in = True
            return self._blog_name
        return None

    # -- Posting --------------------------------------------------------------

    def _can_post(self) -> bool:
        return bool(self.api_key and self.blog and self._consumer_secret
                    and self._oauth_token and self._oauth_token_secret)

    async def create_text_post(self, body: str, title: str = "",
                               tags: list[str] | None = None) -> dict | None:
        """Create a published text post via the OAuth1-signed legacy endpoint.

        Text-only. Returns {id, url} or None. Needs the full OAuth1 user token
        (consumer_secret + oauth_token + oauth_token_secret) — the read-only
        api_key alone can't post.
        """
        import secrets
        import time
        if not self._can_post():
            return None
        url = f"{_API_BASE}/blog/{self.blog}/post"
        params = {"type": "text", "state": "published", "body": body}
        if title:
            params["title"] = title
        if tags:
            params["tags"] = ",".join(tags)
        header = _oauth1_header(
            "POST", url, params,
            consumer_key=self.api_key, consumer_secret=self._consumer_secret,
            token=self._oauth_token, token_secret=self._oauth_token_secret,
            timestamp=int(time.time()), nonce=secrets.token_hex(16))
        try:
            resp = await self._http.post(url, data=params, headers={"Authorization": header})
            if resp.status_code not in (200, 201):
                logger.error("TUM: post failed (%s): %s", resp.status_code, resp.text[:300])
                return None
            j = resp.json()
            post_id = str(((j.get("response") or {}).get("id")) or "")
            host = self.blog if "." in self.blog else f"{self.blog}.tumblr.com"
            return {"id": post_id, "url": f"https://{host}/post/{post_id}" if post_id else ""}
        except Exception as e:
            logger.error("TUM: post error: %s", e)
            return None

    async def ensure_logged_in(self) -> bool:
        if self._logged_in and self._blog_name:
            return True
        return bool(await self.validate_session())

    # -- HTTP Helpers ---------------------------------------------------------

    async def _get_json(self, path: str, params: dict | None = None) -> dict | None:
        url = f"{_API_BASE}{path}"
        try:
            resp = await self._http.get(url, params=params)
            if resp.status_code == 429:
                logger.warning("TUM: Rate limited (429), waiting 30s...")
                await asyncio.sleep(30)
                resp = await self._http.get(url, params=params)
            if resp.status_code == 401:
                logger.error("TUM: Unauthorised (401) — API key invalid")
                return None
            if resp.status_code == 404:
                logger.warning("TUM: Not found (404) for %s", path)
                return None
            resp.raise_for_status()
            return resp.json()
        except httpx.HTTPError as e:
            logger.error("TUM: Failed to fetch %s: %s", path, e)
            return None
        except Exception as e:
            logger.error("TUM: JSON parse error for %s: %s", path, e)
            return None

    # -- Post Discovery -------------------------------------------------------

    async def get_all_post_uris(self) -> list[dict]:
        """Fetch all original posts for the blog, newest first. Returns items
        carrying the raw post under 'post' (the listing already has note_count,
        so the details pass needs no extra round-trip). Offset/limit paging."""
        if not await self.ensure_logged_in():
            logger.error("TUM: Not logged in, cannot fetch posts")
            return []

        all_posts: list[dict] = []
        seen: set[str] = set()
        offset = 0
        limit = 20

        for _page_safety in range(2000):
            data = await self._get_json(
                f"/blog/{self.blog}/posts",
                {"api_key": self.api_key, "limit": str(limit), "offset": str(offset),
                 "notes_info": "false", "reblog_info": "false", "filter": "text"},
            )
            posts = ((data or {}).get("response") or {}).get("posts") if isinstance(data, dict) else None
            if not posts:
                break

            for post in posts:
                pid = str(post.get("id_string") or post.get("id") or "")
                if not pid or pid in seen:
                    continue
                seen.add(pid)
                all_posts.append({"post_uri": pid, "post": post})

            if len(posts) < limit:
                break
            offset += limit
            await asyncio.sleep(config.TUM_REQUEST_DELAY_SECONDS)

        logger.info("TUM: Found %d posts for %s", len(all_posts), self._blog_name or self.blog)
        return all_posts

    # -- Post Details ---------------------------------------------------------

    async def get_post_details_batch(self, items: list[dict]) -> list[dict]:
        """Parse the raw posts gathered in discovery — no extra API calls."""
        details: list[dict] = []
        for item in items:
            post = item.get("post")
            detail = (self._parse_post(post) if post
                      else self._empty_detail(item.get("post_uri", "")))
            details.append(detail)
        return details

    # -- Parsing Helpers ------------------------------------------------------

    def _post_title(self, post: dict) -> str:
        """Best-effort title: explicit title, else summary, else flattened body."""
        for key in ("title", "summary"):
            v = post.get(key)
            if v:
                return _strip_html(str(v))
        body = post.get("body") or post.get("caption") or ""
        text = _strip_html(str(body))
        return (text[:80] + "...") if len(text) > 80 else text

    def _parse_post(self, post: dict) -> dict:
        uri = str(post.get("id_string") or post.get("id") or "")
        title = self._post_title(post)
        ts = post.get("timestamp")
        posted_at = ""
        if ts:
            try:
                posted_at = datetime.fromtimestamp(int(ts), tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
            except (ValueError, OSError, TypeError):
                posted_at = post.get("date", "") or ""

        # Thumbnail — photo posts expose photos[].original_size.url.
        thumbnail_url = ""
        photos = post.get("photos") or []
        if photos:
            orig = (photos[0] or {}).get("original_size") or {}
            thumbnail_url = orig.get("url", "")
        has_media = bool(thumbnail_url) or post.get("type") in ("photo", "video", "audio")

        return {
            "post_uri": uri,
            "title": title or "(untitled)",
            "full_text": _strip_html(str(post.get("body") or post.get("caption") or "")),
            "username": post.get("blog_name", self._blog_name),
            "posted_at": posted_at,
            "content_type": post.get("type", "post"),   # text/photo/quote/link/...
            "rating": "General",
            "description": title,
            "keywords": [t for t in (post.get("tags") or []) if t],
            "link": post.get("post_url", ""),
            "thumbnail_url": thumbnail_url,
            "notes": _safe_int(post.get("note_count", 0)),
            "has_media": 1 if has_media else 0,
            "embed_type": post.get("type", ""),
        }

    def _empty_detail(self, uri: str) -> dict:
        return {
            "post_uri": uri,
            "title": "",
            "full_text": "",
            "username": self._blog_name,
            "posted_at": "",
            "content_type": "post",
            "rating": "General",
            "description": "",
            "keywords": [],
            "link": "",
            "thumbnail_url": "",
            "notes": 0,
            "has_media": 0,
            "embed_type": "",
        }
