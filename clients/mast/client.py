"""Mastodon (MAST) REST API client.

Mastodon is decentralised — every instance (mastodon.social, pawb.fun,
meow.social, snouts.online, …) runs the same open REST API, so the client
is pointed at the user's *instance URL* and authenticates with a personal
**access token** (Settings → Development → New application on the instance,
scopes ``read``). No OAuth dance, no refresh token needed.

Key details:
  - Post IDs are ActivityPub URIs (https://instance/users/x/statuses/123),
    globally unique, stored as TEXT (mirrors the bsky URI scheme).
  - Stats: favourites → likes, reblogs → reposts, replies. Mastodon has no
    native quote count, so quotes is always 0 (kept for schema parity).
  - The statuses timeline already carries the counts, so unlike Bluesky there
    is no second per-post fetch — get_all_post_uris carries the raw status and
    get_post_details_batch just parses it (mirrors the X poller).
  - Reblogs (boosts) are someone else's post; they're dropped UNLESS the
    account is @-mentioned in the boosted post (then kept + flagged 'repost'),
    matching the Bluesky/X pollers.
  - Pagination: max_id cursor (id of the last status seen).
"""

from __future__ import annotations
import asyncio
import html
import logging
import re
from typing import Any

import httpx

import config

logger = logging.getLogger(__name__)

_HEADERS = {
    "User-Agent": "PawPoller/1.0",
    "Accept": "application/json",
}

_TAG_RE = re.compile(r"<[^>]+>")


def _safe_int(val: Any) -> int:
    """Safely convert a value to int, handling None, comma-formatted strings, etc."""
    if val is None:
        return 0
    try:
        if isinstance(val, str):
            val = val.replace(",", "").strip()
        return int(val)
    except (ValueError, TypeError):
        return 0


def _strip_html(body: str) -> str:
    """Mastodon status content is HTML — flatten to plain text for titles."""
    if not body:
        return ""
    # Treat block/line breaks as spaces so words don't run together.
    text = re.sub(r"<br\s*/?>|</p>", " ", body, flags=re.IGNORECASE)
    text = _TAG_RE.sub("", text)
    return html.unescape(text).strip()


_COMPAT_RE = re.compile(r"\(compatible;\s*([A-Za-z][A-Za-z0-9._+-]*)", re.IGNORECASE)


def detect_flavour(version: str = "", title: str = "", source_url: str = "") -> str:
    """Name the fediverse server software from its public `/api/v1/instance`.

    Every Mastodon-API-compatible server (Pleroma, Akkoma, Pixelfed, …) reports
    a version string like ``2.7.2 (compatible; Pleroma 2.10.2)`` — the Mastodon
    API version it emulates, then its own name in the ``(compatible; NAME …)``
    tail. We read that tail. GoToSocial doesn't use the tail, so we fall back to
    its ``source_url``/title; a plain semver with no marker is Mastodon itself.
    Pure string parsing — no model (the NO-AI rule). Returns e.g. "Pleroma",
    "Akkoma", "Pixelfed", "GoToSocial", or "Mastodon"."""
    m = _COMPAT_RE.search(version or "")
    if m:
        return m.group(1)
    src = (source_url or "").lower()
    blob = f"{src} {(title or '').lower()}"
    if "gotosocial" in blob:
        return "GoToSocial"
    if "pixelfed" in blob:
        return "Pixelfed"
    return "Mastodon"


def _normalise_instance(url: str) -> str:
    """Normalise an instance URL to ``https://host`` (no trailing slash/path)."""
    url = (url or "").strip()
    if not url:
        return ""
    if not url.startswith("http://") and not url.startswith("https://"):
        url = "https://" + url
    return url.rstrip("/")


def _status_mentions_account(status: dict, account_id: str) -> bool:
    """True if *account_id* is @-mentioned in the status. Used to keep reblogs
    that actually tag the account (mirrors the bsky/X pollers)."""
    if not account_id:
        return False
    for m in (status or {}).get("mentions", []) or []:
        if str(m.get("id")) == str(account_id):
            return True
    return False


class MastClient:
    """Async HTTP client for a Mastodon instance's REST API."""

    def __init__(self, instance_url: str = "", access_token: str = "",
                 proxy_url: str = "", proxy_key: str = ""):
        self.instance_url = _normalise_instance(instance_url)
        self.access_token = access_token
        self._account_id: str = ""
        self._handle: str = ""          # @user@instance
        self._username: str = ""
        self._logged_in = False

        # Optional CF Worker proxy — opt-in backup, not required from any IP
        # today. Mirrors the bsky client; enabled via mast_use_cf_proxy.
        if proxy_url and proxy_key:
            from polling.cf_proxy import CloudflareProxyTransport
            transport = CloudflareProxyTransport(proxy_url, proxy_key)
            logger.info("Mast client using CF proxy: %s", proxy_url)
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

    def update_credentials(self, instance_url: str, access_token: str) -> None:
        """Update stored credentials. Resets login state if changed."""
        new_instance = _normalise_instance(instance_url)
        changed = (self.instance_url != new_instance or self.access_token != access_token)
        self.instance_url = new_instance
        self.access_token = access_token
        if changed:
            self._logged_in = False
            self._account_id = ""
            self._handle = ""
            self._username = ""

    # -- Auth -----------------------------------------------------------------

    async def validate_session(self) -> str | None:
        """Verify the token against verify_credentials. Returns the handle
        (@user@instance) on success, the account id is cached for polling."""
        if not self.instance_url or not self.access_token:
            return None
        data = await self._get_json("/api/v1/accounts/verify_credentials")
        if data and isinstance(data, dict) and data.get("id"):
            self._account_id = str(data["id"])
            self._username = data.get("username", "")
            # acct is bare username on the home instance; build a full handle.
            host = self.instance_url.split("://", 1)[-1]
            acct = data.get("acct", self._username)
            self._handle = f"@{acct}@{host}" if "@" not in acct else f"@{acct}"
            self._logged_in = True
            return self._handle
        return None

    async def get_follower_count(self) -> int | None:
        """Return the authenticated account's follower count via verify_credentials."""
        data = await self._get_json("/api/v1/accounts/verify_credentials")
        if data and isinstance(data, dict) and data.get("followers_count") is not None:
            return _safe_int(data.get("followers_count"))
        return None

    async def ensure_logged_in(self) -> bool:
        if self._logged_in and self._account_id:
            return True
        return bool(await self.validate_session())

    async def get_instance_info(self) -> dict:
        """Public instance metadata + detected server flavour (see
        ``detect_flavour``). Returns ``{"software": str, "version": str}``;
        empty software if the instance couldn't be read."""
        data = await self._get_json("/api/v1/instance")
        if not isinstance(data, dict):
            return {"software": "", "version": ""}
        version = str(data.get("version", "") or "")
        return {
            "software": detect_flavour(version, str(data.get("title", "") or ""),
                                       str(data.get("source_url", "") or "")),
            "version": version,
        }

    # -- HTTP Helpers ---------------------------------------------------------

    async def _get_json(self, path: str, params: dict | None = None) -> dict | list | None:
        """GET a JSON endpoint on the instance with Bearer auth + error handling."""
        url = f"{self.instance_url}{path}"
        headers = {"Authorization": f"Bearer {self.access_token}"}
        try:
            resp = await self._http.get(url, params=params, headers=headers)

            if resp.status_code == 429:
                logger.warning("MAST: Rate limited (429), waiting 30s...")
                await asyncio.sleep(30)
                resp = await self._http.get(url, params=params, headers=headers)

            if resp.status_code == 401:
                logger.error("MAST: Unauthorised (401) — token invalid or revoked")
                return None
            if resp.status_code == 404:
                logger.warning("MAST: Not found (404) for %s", path)
                return None

            resp.raise_for_status()
            return resp.json()
        except httpx.HTTPError as e:
            logger.error("MAST: Failed to fetch %s: %s", path, e)
            return None
        except Exception as e:
            logger.error("MAST: JSON parse error for %s: %s", path, e)
            return None

    # -- Post Discovery -------------------------------------------------------

    async def get_all_post_uris(self) -> list[dict]:
        """Fetch all statuses for the authenticated account, newest first.

        Returns a list of dicts carrying the raw status under 'status' (so the
        details pass needs no extra round-trip). Reblogs are dropped unless the
        account is @-tagged in the boosted post (then kept + flagged 'repost').
        Pagination is max_id-based.
        """
        if not await self.ensure_logged_in():
            logger.error("MAST: Not logged in, cannot fetch statuses")
            return []

        all_posts: list[dict] = []
        seen: set[str] = set()
        max_id: str | None = None

        for _page_safety in range(1000):
            params: dict[str, str] = {
                "limit": "40",
                # Keep replies (comments by you); reblogs handled per-item below.
                "exclude_replies": "false",
                "exclude_reblogs": "false",
            }
            if max_id:
                params["max_id"] = max_id

            data = await self._get_json(
                f"/api/v1/accounts/{self._account_id}/statuses",
                params=params,
            )
            if not data or not isinstance(data, list):
                break
            if not data:
                break

            for status in data:
                max_id = status.get("id", max_id)   # advance cursor regardless
                reblog = status.get("reblog")
                is_repost = isinstance(reblog, dict)
                # A boost wraps someone else's status — track it only when the
                # account is tagged in the original, and then track the original.
                if is_repost:
                    if not _status_mentions_account(reblog, self._account_id):
                        continue
                    target = reblog
                else:
                    target = status

                uri = target.get("uri", "") or target.get("url", "")
                if not uri or uri in seen:
                    continue
                seen.add(uri)
                entry = {"post_uri": uri, "status": target}
                if is_repost:
                    entry["content_type"] = "repost"
                all_posts.append(entry)

            if len(data) < 40:
                break
            await asyncio.sleep(config.MAST_REQUEST_DELAY_SECONDS)

        logger.info("MAST: Found %d statuses for %s", len(all_posts), self._handle)
        return all_posts

    # -- Post Details ---------------------------------------------------------

    async def get_post_details_batch(self, items: list[dict]) -> list[dict]:
        """Parse the raw statuses gathered in discovery. No extra API calls —
        the Mastodon timeline already carries all counts (mirrors the X poller).
        """
        details: list[dict] = []
        for item in items:
            status = item.get("status")
            detail = (self._parse_status(status) if status
                      else self._empty_detail(item.get("post_uri", "")))
            if item.get("content_type"):
                detail["content_type"] = item["content_type"]
            details.append(detail)
        return details

    # -- Parsing Helpers ------------------------------------------------------

    def _parse_status(self, status: dict) -> dict:
        """Parse a Mastodon status object into a normalised detail dict."""
        uri = status.get("uri", "") or status.get("url", "")
        link = status.get("url", "") or uri
        account = status.get("account", {}) or {}
        handle = account.get("acct", self._username)
        text = _strip_html(status.get("content", ""))

        # Content type — reblog flag (set at discovery) overrides this later.
        if status.get("in_reply_to_id"):
            content_type = "reply"
        elif status.get("quote") or status.get("quote_id"):
            content_type = "quote"
        else:
            content_type = "post"

        media = status.get("media_attachments", []) or []
        has_media = bool(media)
        thumbnail_url = ""
        if media:
            first = media[0] or {}
            thumbnail_url = first.get("preview_url", "") or first.get("url", "")
        embed_type = (media[0].get("type", "") if media else "")

        keywords = [t.get("name", "") for t in (status.get("tags", []) or []) if t.get("name")]

        # Sensitive flag → rough rating; CWs map to a Mature-ish marker.
        rating = "Mature" if status.get("sensitive") else "General"

        return {
            "post_uri": uri,
            "title": text[:80] + ("..." if len(text) > 80 else "") if text else "",
            "full_text": text,
            "username": handle,
            "posted_at": status.get("created_at", ""),
            "content_type": content_type,
            "rating": rating,
            "description": text,
            "keywords": keywords,
            "link": link,
            "thumbnail_url": thumbnail_url,
            "likes": _safe_int(status.get("favourites_count", 0)),
            "reposts": _safe_int(status.get("reblogs_count", 0)),
            "replies": _safe_int(status.get("replies_count", 0)),
            "quotes": 0,   # Mastodon has no native quote count
            "has_media": 1 if has_media else 0,
            "embed_type": embed_type,
        }

    def _empty_detail(self, uri: str) -> dict:
        """Return an empty detail dict for a status that couldn't be parsed."""
        return {
            "post_uri": uri,
            "title": "",
            "full_text": "",
            "username": self._username,
            "posted_at": "",
            "content_type": "post",
            "rating": "General",
            "description": "",
            "keywords": [],
            "link": uri,
            "thumbnail_url": "",
            "likes": 0,
            "reposts": 0,
            "replies": 0,
            "quotes": 0,
            "has_media": 0,
            "embed_type": "",
        }

    async def get_status_context(self, status_id: str) -> list[dict]:
        """All replies under one of our statuses (gap G3 inbox capture).

        One official ``GET /api/v1/statuses/{id}/context`` call; descendants are
        every reply in the thread. Returns one dict per reply:
        {comment_id, author, body, commented_at, permalink}.
        """
        if not await self.ensure_logged_in():
            return []
        data = await self._get_json(f"/api/v1/statuses/{status_id}/context")
        if not isinstance(data, dict):
            return []
        out = []
        for st in data.get("descendants") or []:
            sid = str(st.get("id") or "")
            if not sid:
                continue
            acct = (st.get("account") or {})
            out.append({
                "comment_id": sid,
                "author": acct.get("acct") or acct.get("username", ""),
                "body": _strip_html(st.get("content", "")),
                "commented_at": st.get("created_at", ""),
                "permalink": st.get("url") or st.get("uri", ""),
            })
        return out

    # -- Posting (Posts module) -----------------------------------------------

    async def _upload_media(self, image_path: str, description: str = "") -> str | None:
        """Upload one image to /api/v2/media, return its media id (or None).

        v2 media may reply 200 (ready) or 202 (still processing) — either way the
        id is usable straight away; Mastodon holds the status until the media is
        processed, so we don't need to poll.
        """
        import mimetypes
        import os
        mime = mimetypes.guess_type(image_path)[0] or "image/jpeg"
        headers = {"Authorization": f"Bearer {self.access_token}"}
        try:
            with open(image_path, "rb") as f:
                files = {"file": (os.path.basename(image_path), f, mime)}
                data = {"description": description} if description else None
                resp = await self._http.post(
                    f"{self.instance_url}/api/v2/media",
                    files=files, data=data, headers=headers, timeout=120.0,
                )
            if resp.status_code not in (200, 202):
                logger.error("MAST: media upload failed (%s): %s",
                             resp.status_code, resp.text[:200])
                return None
            return str((resp.json() or {}).get("id") or "") or None
        except Exception as e:
            logger.error("MAST: media upload error: %s", e)
            return None

    async def create_status(self, text: str, *, image_path: str | None = None,
                            image_alt: str = "", image_paths: list[str] | None = None,
                            image_alts: list[str] | None = None, sensitive: bool = False,
                            visibility: str = "public",
                            idempotency_key: str = "",
                            in_reply_to_id: str = "") -> dict | None:
        """Publish a status (a "toot"). Returns {id, uri, url} on success.

        Requires a token with a **write** scope (the poll-only token minted with
        scope=read will 403 here — surfaced to the caller as an error).
        ``in_reply_to_id`` threads the status as a reply (gap G3 native reply).
        """
        if not await self.ensure_logged_in():
            logger.error("MAST: not logged in, cannot post")
            return None

        # Up to 4 images. Prefer the multi-image params, falling back to the
        # legacy single image_path/image_alt so older callers still work.
        paths = list(image_paths) if image_paths else ([image_path] if image_path else [])
        alts = list(image_alts) if image_alts else ([image_alt] if image_path else [])
        media_ids: list[str] = []
        for i, pth in enumerate(paths[:4]):
            mid = await self._upload_media(pth, alts[i] if i < len(alts) else "")
            if not mid:
                return None   # image was requested but couldn't be attached
            media_ids.append(mid)

        payload: dict = {"status": text, "visibility": visibility}
        if sensitive:
            payload["sensitive"] = "true"
        if media_ids:
            payload["media_ids[]"] = media_ids
        if in_reply_to_id:
            payload["in_reply_to_id"] = in_reply_to_id

        headers = {"Authorization": f"Bearer {self.access_token}"}
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key
        try:
            resp = await self._http.post(
                f"{self.instance_url}/api/v1/statuses",
                data=payload, headers=headers, timeout=60.0,
            )
            if resp.status_code == 403:
                logger.error("MAST: post rejected (403) — token lacks a write scope")
                return None
            resp.raise_for_status()
            status = resp.json() or {}
            result = {
                "id": str(status.get("id", "")),
                "uri": status.get("uri", "") or status.get("url", ""),
                "url": status.get("url", "") or status.get("uri", ""),
            }
            logger.info("MAST: posted status %s", result["url"])
            return result
        except Exception as e:
            logger.error("MAST: post failed: %s", e)
            return None
