"""Bluesky (BSKY) AT Protocol API client.

Bluesky provides a free public API via the AT Protocol. Authentication
uses app passwords to obtain JWT sessions (accessJwt + refreshJwt).

Key details:
  - Post IDs are AT URIs (at://did:plc:xxx/app.bsky.feed.post/yyy)
  - Stats: likes, reposts, replies, quotes (NO views/impressions)
  - Auth: App Password → JWT via com.atproto.server.createSession
  - Session management: accessJwt/refreshJwt with auto-refresh
  - Pagination: cursor-based (cursor field in response)
"""

from __future__ import annotations
import asyncio
import logging
from typing import Any

import httpx

import config

logger = logging.getLogger(__name__)

_API_BASE = "https://bsky.social/xrpc"

_HEADERS = {
    "User-Agent": "PawPoller/1.0",
    "Accept": "application/json",
}


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


def _is_repost_item(item: dict) -> bool:
    """True if a getAuthorFeed item is a repost (the actor reposted someone
    else's post). Such items carry a `reason` of type ``…#reasonRepost`` and
    their ``post`` is the ORIGINAL author's — whose likes/reposts/replies aren't
    the actor's, so tracking them would pollute the dashboard. Pinned posts
    (``reasonPin``) are the actor's own and are NOT treated as reposts.
    """
    reason = item.get("reason")
    if not isinstance(reason, dict):
        return False
    return "reasonRepost" in (reason.get("$type") or "")


def _post_mentions_did(post: dict, did: str) -> bool:
    """True if *did* is @-mentioned in the post's rich-text facets. Used to keep
    reposts that actually tag the account (mirrors the X poller)."""
    if not did:
        return False
    record = (post or {}).get("record", {}) or {}
    for facet in record.get("facets", []) or []:
        for feat in facet.get("features", []) or []:
            if feat.get("$type") == "app.bsky.richtext.facet#mention" and feat.get("did") == did:
                return True
    return False


class BskyClient:
    """Async HTTP client for Bluesky's AT Protocol API."""

    def __init__(self, identifier: str = "", app_password: str = "",
                 proxy_url: str = "", proxy_key: str = ""):
        self.identifier = identifier      # Handle (user.bsky.social) or DID
        self.app_password = app_password   # App password (NOT main account password)
        self._access_jwt: str = ""
        self._refresh_jwt: str = ""
        self._did: str = ""
        self._handle: str = ""
        self._logged_in = False

        # Optional CF Worker proxy — opt-in backup, not required from
        # any IP today. Enabled via bsky_use_cf_proxy if Bluesky ever
        # starts blocking datacenter IPs.
        if proxy_url and proxy_key:
            from polling.cf_proxy import CloudflareProxyTransport
            transport = CloudflareProxyTransport(proxy_url, proxy_key)
            logger.info("Bsky client using CF proxy: %s", proxy_url)
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

    def update_credentials(self, identifier: str, app_password: str) -> None:
        """Update stored credentials. Resets login state if changed."""
        changed = (self.identifier != identifier or self.app_password != app_password)
        self.identifier = identifier
        self.app_password = app_password
        if changed:
            self._logged_in = False
            self._access_jwt = ""
            self._refresh_jwt = ""
            self._did = ""
            self._handle = ""

    # -- Authentication -------------------------------------------------------

    async def login(self) -> bool:
        """Authenticate via com.atproto.server.createSession.

        Posts identifier + app_password to the createSession endpoint.
        On success, stores accessJwt, refreshJwt, and DID for subsequent
        authenticated requests.
        """
        if not self.identifier or not self.app_password:
            return False
        try:
            resp = await self._http.post(
                f"{_API_BASE}/com.atproto.server.createSession",
                json={"identifier": self.identifier, "password": self.app_password},
            )
            if resp.status_code != 200:
                logger.error("BSKY: Login failed (status %d): %s", resp.status_code, resp.text[:200])
                return False
            data = resp.json()
            self._access_jwt = data.get("accessJwt", "")
            self._refresh_jwt = data.get("refreshJwt", "")
            self._did = data.get("did", "")
            self._handle = data.get("handle", "")
            self._logged_in = True
            logger.info("BSKY: Login successful for %s (did=%s)", self._handle, self._did)
            return True
        except Exception as e:
            logger.error("BSKY: Login failed: %s", e)
            return False

    async def refresh_session(self) -> bool:
        """Refresh the access token using the refresh JWT."""
        if not self._refresh_jwt:
            return False
        try:
            resp = await self._http.post(
                f"{_API_BASE}/com.atproto.server.refreshSession",
                headers={"Authorization": f"Bearer {self._refresh_jwt}"},
            )
            if resp.status_code != 200:
                logger.warning("BSKY: Session refresh failed (status %d)", resp.status_code)
                return False
            data = resp.json()
            self._access_jwt = data.get("accessJwt", "")
            self._refresh_jwt = data.get("refreshJwt", "")
            self._did = data.get("did", self._did)
            self._handle = data.get("handle", self._handle)
            logger.info("BSKY: Session refreshed for %s", self._handle)
            return True
        except Exception as e:
            logger.warning("BSKY: Session refresh failed: %s", e)
            return False

    async def check_session(self) -> bool:
        """Check if the current access token is still valid."""
        if not self._access_jwt:
            return False
        try:
            resp = await self._http.get(
                f"{_API_BASE}/com.atproto.server.getSession",
                headers={"Authorization": f"Bearer {self._access_jwt}"},
            )
            return resp.status_code == 200
        except Exception:
            return False

    async def ensure_logged_in(self) -> bool:
        """Check session → refresh → login fallback chain."""
        if await self.check_session():
            return True
        if await self.refresh_session():
            return True
        self._logged_in = False
        return await self.login()

    async def validate_session(self) -> str | None:
        """Login and verify the account works. Returns handle on success."""
        if not await self.ensure_logged_in():
            return None
        try:
            data = await self._get_json(
                f"{_API_BASE}/app.bsky.actor.getProfile",
                params={"actor": self._did},
            )
            if data and isinstance(data, dict):
                self._handle = data.get("handle", self._handle)
                return self._handle
        except Exception as e:
            logger.warning("BSKY: Profile validation failed: %s", e)
        return None

    async def get_follower_count(self) -> int | None:
        """Return the authenticated account's follower count via getProfile."""
        if not await self.ensure_logged_in():
            return None
        try:
            data = await self._get_json(
                f"{_API_BASE}/app.bsky.actor.getProfile",
                params={"actor": self._did},
            )
        except Exception as e:
            logger.warning("BSKY: follower fetch failed: %s", e)
            return None
        if data and isinstance(data, dict) and data.get("followersCount") is not None:
            return _safe_int(data.get("followersCount"))
        return None

    # -- HTTP Helpers ---------------------------------------------------------

    async def _get_json(self, url: str, params: dict | None = None) -> dict | list | None:
        """Fetch a JSON endpoint with auth header injection and error handling."""
        headers = {}
        if self._access_jwt:
            headers["Authorization"] = f"Bearer {self._access_jwt}"
        try:
            resp = await self._http.get(url, params=params, headers=headers)

            # Auto-refresh on 401
            if resp.status_code == 401:
                logger.info("BSKY: Got 401, attempting session refresh...")
                if await self.refresh_session():
                    headers["Authorization"] = f"Bearer {self._access_jwt}"
                    resp = await self._http.get(url, params=params, headers=headers)
                else:
                    logger.error("BSKY: Session refresh failed after 401")
                    return None

            if resp.status_code == 429:
                logger.warning("BSKY: Rate limited (429), waiting 30s...")
                await asyncio.sleep(30)
                resp = await self._http.get(url, params=params, headers=headers)

            if resp.status_code == 404:
                logger.warning("BSKY: Not found (404) for %s", url)
                return None

            resp.raise_for_status()
            return resp.json()
        except httpx.HTTPError as e:
            logger.error("BSKY: Failed to fetch %s: %s", url, e)
            return None
        except Exception as e:
            logger.error("BSKY: JSON parse error for %s: %s", url, e)
            return None

    # -- Post Discovery -------------------------------------------------------

    async def get_all_post_uris(self) -> list[dict]:
        """Fetch all post URIs for the authenticated user via getAuthorFeed.

        Returns a list of dicts with 'post_uri' and 'title' keys.
        Uses cursor-based pagination.
        """
        if not await self.ensure_logged_in():
            logger.error("BSKY: Not logged in, cannot fetch posts")
            return []

        all_posts: list[dict] = []
        seen_uris: set[str] = set()
        cursor: str | None = None

        for _page_safety in range(1000):
            params: dict[str, str] = {
                "actor": self._did,
                "limit": "100",
                # Include the actor's replies (comments by you) — reposts are
                # dropped below. Matches the X poller: own posts + replies, no
                # reposts.
                "filter": "posts_with_replies",
            }
            if cursor:
                params["cursor"] = cursor

            data = await self._get_json(
                f"{_API_BASE}/app.bsky.feed.getAuthorFeed",
                params=params,
            )
            if not data or not isinstance(data, dict):
                break

            feed = data.get("feed", [])
            if not feed:
                break

            for item in feed:
                # Reposts are dropped UNLESS the account is tagged in them (then
                # they're kept and flagged so they show as a "Repost"). Their
                # stats are the original author's — mirrors the X poller.
                is_repost = _is_repost_item(item)
                post = item.get("post", {})
                if is_repost and not _post_mentions_did(post, self._did):
                    continue
                uri = post.get("uri", "")
                if uri and uri not in seen_uris:
                    seen_uris.add(uri)
                    # Use first 80 chars of text as title
                    record = post.get("record", {})
                    text = record.get("text", "")
                    title = text[:80] + ("..." if len(text) > 80 else "") if text else ""
                    entry = {"post_uri": uri, "title": title}
                    if is_repost:
                        entry["content_type"] = "repost"   # threaded through to upsert
                    all_posts.append(entry)

            cursor = data.get("cursor")
            if not cursor:
                break

            await asyncio.sleep(config.BSKY_REQUEST_DELAY_SECONDS)

        logger.info("BSKY: Found %d posts for %s", len(all_posts), self._handle)
        return all_posts

    # -- Post Details ---------------------------------------------------------

    async def get_post_detail(self, uri: str) -> dict:
        """Fetch stats and metadata for a single post via getPosts.

        Returns a dict with post metadata and engagement stats.
        """
        data = await self._get_json(
            f"{_API_BASE}/app.bsky.feed.getPosts",
            params={"uris": uri},
        )

        if not data or not isinstance(data, dict):
            return self._empty_detail(uri)

        posts = data.get("posts", [])
        if not posts:
            return self._empty_detail(uri)

        return self._parse_post(posts[0])

    async def get_post_details_batch(self, items: list[dict]) -> list[dict]:
        """Fetch details for multiple posts using batched getPosts calls.

        The getPosts endpoint accepts up to 25 URIs per request.
        """
        details: list[dict] = []
        batch_size = 25

        for i in range(0, len(items), batch_size):
            if i > 0:
                await asyncio.sleep(config.BSKY_REQUEST_DELAY_SECONDS)

            batch = items[i:i + batch_size]
            uris = [item["post_uri"] for item in batch]

            try:
                data = await self._get_json(
                    f"{_API_BASE}/app.bsky.feed.getPosts",
                    params={"uris": uris},
                )

                if data and isinstance(data, dict):
                    posts = data.get("posts", [])
                    # Build URI→post map for matching
                    post_map = {p.get("uri", ""): p for p in posts}
                    for item in batch:
                        post_data = post_map.get(item["post_uri"])
                        detail = (self._parse_post(post_data) if post_data
                                  else self._empty_detail(item["post_uri"]))
                        # Repost flag is known only at discovery (feed reason) —
                        # override the parsed type so it shows as a "Repost".
                        if item.get("content_type"):
                            detail["content_type"] = item["content_type"]
                        details.append(detail)
                else:
                    # Batch failed — add empty details
                    for item in batch:
                        details.append(self._empty_detail(item["post_uri"]))

            except Exception as e:
                logger.warning("BSKY: Failed to fetch batch at offset %d: %s", i, e)
                for item in batch:
                    details.append(self._empty_detail(item["post_uri"]))

        return details

    async def get_post_thread(self, uri: str, depth: int = 6) -> list[dict]:
        """All replies under one of our posts, flattened (gap G3 inbox capture).

        One ``app.bsky.feed.getPostThread`` call. Returns one dict per reply:
        {comment_id (=reply uri), author, body, commented_at, permalink,
         cid, root_uri, root_cid} — the uri/cid pairs are exactly what a native
        threaded reply needs (create_post's ``reply`` refs).
        """
        if not await self.ensure_logged_in():
            return []
        data = await self._get_json(
            f"{_API_BASE}/app.bsky.feed.getPostThread",
            params={"uri": uri, "depth": depth},
        )
        thread = (data or {}).get("thread") if isinstance(data, dict) else None
        if not thread:
            return []
        root_post = thread.get("post", {}) or {}
        root_uri = root_post.get("uri", uri)
        root_cid = root_post.get("cid", "")
        out: list[dict] = []

        def _walk(node: dict):
            for child in node.get("replies") or []:
                post = child.get("post") or {}
                p_uri = post.get("uri", "")
                if not p_uri:
                    continue
                author = (post.get("author") or {})
                handle = author.get("handle", "")
                rkey = p_uri.rsplit("/", 1)[-1]
                out.append({
                    "comment_id": p_uri,
                    "author": handle or author.get("displayName", ""),
                    "body": (post.get("record") or {}).get("text", ""),
                    "commented_at": (post.get("record") or {}).get("createdAt", ""),
                    "permalink": f"https://bsky.app/profile/{handle}/post/{rkey}" if handle else "",
                    "cid": post.get("cid", ""),
                    "root_uri": root_uri,
                    "root_cid": root_cid,
                })
                _walk(child)

        _walk(thread)
        return out

    # -- Parsing Helpers ------------------------------------------------------

    def _parse_post(self, post: dict) -> dict:
        """Parse a post object from getPosts response into a normalised detail dict."""
        uri = post.get("uri", "")
        rkey = uri.rsplit("/", 1)[-1] if "/" in uri else uri
        author = post.get("author", {})
        handle = author.get("handle", self._handle)
        record = post.get("record", {})
        text = record.get("text", "")

        # Determine content type. A repost flag (set at discovery) overrides this
        # later in get_post_details_batch; here we tell apart reply/quote/post.
        embed = post.get("embed", {})
        embed_type = embed.get("$type", "") if isinstance(embed, dict) else ""
        has_media = bool(embed_type)
        if record.get("reply"):
            content_type = "reply"
        elif "embed.record" in embed_type:   # record / recordWithMedia = quote
            content_type = "quote"
        else:
            content_type = "post"

        # Build post link
        link = f"https://bsky.app/profile/{handle}/post/{rkey}"

        # Stats
        likes = _safe_int(post.get("likeCount", 0))
        reposts = _safe_int(post.get("repostCount", 0))
        replies = _safe_int(post.get("replyCount", 0))
        quotes = _safe_int(post.get("quoteCount", 0))

        # Tags/facets as keywords
        facets = record.get("facets", [])
        keywords = []
        if isinstance(facets, list):
            for facet in facets:
                features = facet.get("features", [])
                for feat in features:
                    if feat.get("$type") == "app.bsky.richtext.facet#tag":
                        tag = feat.get("tag", "")
                        if tag:
                            keywords.append(tag)

        # Thumbnail (first image) + the full set of full-res image URLs. A
        # Bluesky post can attach up to 4 images; `media_urls` keeps every one at
        # `fullsize` so the artwork importer can bring them all in (not just the
        # first). `thumbnail_url` stays the first image's thumb for the gallery.
        thumbnail_url = ""
        media_urls: list[str] = []
        img_list = []
        if embed_type == "app.bsky.embed.images#view":
            img_list = embed.get("images", []) or []
        elif embed_type == "app.bsky.embed.external#view":
            ext = embed.get("external", {})
            thumbnail_url = ext.get("thumb", "")
        elif embed_type == "app.bsky.embed.recordWithMedia#view":
            # Quote-with-media: the image is under embed.media.
            media = embed.get("media", {}) or {}
            if media.get("$type") == "app.bsky.embed.images#view":
                img_list = media.get("images", []) or []
        if img_list:
            thumbnail_url = img_list[0].get("thumb", "")
            media_urls = [im.get("fullsize", "") or im.get("thumb", "")
                          for im in img_list]
            media_urls = [u for u in media_urls if u]

        return {
            "post_uri": uri,
            "title": text[:80] + ("..." if len(text) > 80 else "") if text else "",
            "full_text": text,
            "username": handle,
            "posted_at": record.get("createdAt", ""),
            "content_type": content_type,
            "rating": "General",
            "description": text,
            "keywords": keywords,
            "link": link,
            "thumbnail_url": thumbnail_url,
            "media_urls": media_urls,
            "likes": likes,
            "reposts": reposts,
            "replies": replies,
            "quotes": quotes,
            "has_media": 1 if has_media else 0,
            "embed_type": embed_type,
        }

    def _empty_detail(self, uri: str) -> dict:
        """Return an empty detail dict for a post that couldn't be fetched."""
        rkey = uri.rsplit("/", 1)[-1] if "/" in uri else uri
        return {
            "post_uri": uri,
            "title": "",
            "full_text": "",
            "username": self._handle,
            "posted_at": "",
            "content_type": "post",
            "rating": "General",
            "description": "",
            "keywords": [],
            "link": f"https://bsky.app/profile/{self._handle}/post/{rkey}",
            "thumbnail_url": "",
            "media_urls": [],
            "likes": 0,
            "reposts": 0,
            "replies": 0,
            "quotes": 0,
            "has_media": 0,
            "embed_type": "",
        }

    # -- Posting ----------------------------------------------------------------

    async def _post_json(self, url: str, json_data: dict) -> dict | None:
        """POST a JSON endpoint with auth injection and 401 auto-refresh."""
        headers = {}
        if self._access_jwt:
            headers["Authorization"] = f"Bearer {self._access_jwt}"
        try:
            resp = await self._http.post(url, json=json_data, headers=headers)
            if resp.status_code == 401:
                logger.info("BSKY: Got 401 on POST, attempting session refresh...")
                if await self.refresh_session():
                    headers["Authorization"] = f"Bearer {self._access_jwt}"
                    resp = await self._http.post(url, json=json_data, headers=headers)
                else:
                    logger.error("BSKY: Session refresh failed after 401")
                    return None
            if resp.status_code == 429:
                logger.warning("BSKY: Rate limited (429) on POST, waiting 30s...")
                await asyncio.sleep(30)
                resp = await self._http.post(url, json=json_data, headers=headers)
            if resp.status_code >= 400:
                # Surface the PDS's actual reason (e.g. BlobTooLarge) — the bare
                # httpx status is otherwise opaque.
                logger.error("BSKY: POST %s -> %s: %s", url, resp.status_code, resp.text[:300])
            resp.raise_for_status()
            return resp.json()
        except httpx.HTTPError as e:
            logger.error("BSKY: POST failed for %s: %s", url, e)
            return None

    @staticmethod
    def _fit_blob(path: str) -> tuple[str, str | None]:
        """Return an image path within Bluesky's ~1 MB blob cap.

        Bluesky rejects a feed-post image blob over ~976 KB at createRecord time
        (BlobTooLarge → 400) even though uploadBlob itself accepts it. If the file
        is already under the cap and a natively-supported type, returns
        ``(path, None)``; otherwise downscales/re-encodes to JPEG in a temp file
        and returns ``(temp_path, temp_path)`` so the caller can delete it. Mirrors
        the stories path's `_prepare_bsky_image` (kept local to avoid a
        posting→clients import cycle)."""
        import os
        _LIMIT = 950_000
        try:
            size = os.path.getsize(path)
        except OSError:
            return path, None
        ext = os.path.splitext(path)[1].lower()
        if size <= _LIMIT and ext in (".jpg", ".jpeg", ".png", ".gif"):
            return path, None
        try:
            import tempfile
            from PIL import Image
            img = Image.open(path)
            if img.mode in ("RGBA", "LA", "P"):
                img = img.convert("RGBA")
                bg = Image.new("RGB", img.size, (255, 255, 255))
                bg.paste(img, mask=img.split()[-1])
                img = bg
            else:
                img = img.convert("RGB")
            fd, tmp = tempfile.mkstemp(suffix=".jpg", prefix="bsky_")
            os.close(fd)
            quality, max_dim = 90, 2048
            while True:
                work = img
                if max(work.size) > max_dim:
                    ratio = max_dim / max(work.size)
                    work = work.resize((max(1, int(work.size[0] * ratio)),
                                        max(1, int(work.size[1] * ratio))))
                work.save(tmp, "JPEG", quality=quality, optimize=True)
                if os.path.getsize(tmp) <= _LIMIT or (quality <= 40 and max_dim <= 1024):
                    break
                if quality > 40:
                    quality -= 10
                else:
                    max_dim = int(max_dim * 0.8)
            return tmp, tmp
        except Exception as e:
            logger.warning("BSKY: image downscale failed (%s); using original", e)
            return path, None

    async def upload_blob(self, file_path: str, mime_type: str = "image/jpeg") -> dict | None:
        """Upload a blob (image/video) and return the blob reference.

        Args:
            file_path: Absolute path to the file to upload.
            mime_type: MIME type of the file (e.g. "image/jpeg", "image/png").

        Returns:
            The blob dict (with 'ref' and 'mimeType') on success, None on failure.
        """
        if not await self.ensure_logged_in():
            logger.error("BSKY: Not logged in, cannot upload blob")
            return None

        with open(file_path, "rb") as f:
            file_data = f.read()

        headers = {
            "Authorization": f"Bearer {self._access_jwt}",
            "Content-Type": mime_type,
        }
        try:
            resp = await self._http.post(
                f"{_API_BASE}/com.atproto.repo.uploadBlob",
                content=file_data,
                headers=headers,
                timeout=60.0,
            )
            resp.raise_for_status()
            data = resp.json()
            blob = data.get("blob")
            if blob:
                logger.info("BSKY: Uploaded blob (%d bytes, %s)", len(file_data), mime_type)
            return blob
        except Exception as e:
            logger.error("BSKY: Blob upload failed: %s", e)
            return None

    async def create_post(
        self,
        text: str,
        *,
        image_path: str | None = None,
        image_alt: str = "",
        image_paths: list[str] | None = None,
        image_alts: list[str] | None = None,
        labels: list[str] | None = None,
        mention_handles: list[str] | None = None,
        reply: dict | None = None,
    ) -> dict | None:
        """Create a new Bluesky post.

        Args:
            text: Post text (max 300 graphemes).
            image_path: Optional path to a single image to embed (legacy).
            image_alt: Alt text for the single image.
            image_paths: Up to 4 image paths to embed; takes precedence over
                image_path when given.
            image_alts: Per-image alt text, index-aligned to image_paths.
            labels: Content labels (e.g. ["sexual", "nudity"]).
            mention_handles: Bluesky handles (e.g. ["name.bsky.social"]) present
                in ``text`` as ``@handle`` that should become clickable mention
                facets. Each is resolved to a DID; unresolvable ones are skipped.
            reply: Optional reply refs (gap G3 native reply) —
                ``{"root": {"uri", "cid"}, "parent": {"uri", "cid"}}``. Root is
                the thread's first post, parent is the comment being answered.

        Returns:
            Dict with 'uri' and 'cid' on success, None on failure.
        """
        if not await self.ensure_logged_in():
            logger.error("BSKY: Not logged in, cannot create post")
            return None

        from datetime import datetime, timezone
        now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

        record: dict = {
            "$type": "app.bsky.feed.post",
            "text": text,
            "createdAt": now,
        }
        if reply and reply.get("root") and reply.get("parent"):
            record["reply"] = reply

        # Rich-text facets: links + #hashtags + @mentions. Bluesky (unlike
        # X/Mastodon) does NOT auto-link these — without facets a #tag/@handle is
        # dead plain text — so we build them explicitly with UTF-8 byte offsets.
        facets = await self._build_facets(text, mention_handles)
        if facets:
            record["facets"] = facets

        # Embed images (up to 4). Prefer the multi-image params, falling back to
        # the legacy single image_path/image_alt so older callers still work.
        paths = list(image_paths) if image_paths else ([image_path] if image_path else [])
        alts = list(image_alts) if image_alts else ([image_alt] if image_path else [])
        images = []
        _tmps: list[str] = []
        for i, pth in enumerate(paths[:4]):
            use_path, tmp = self._fit_blob(pth)   # keep each image under Bluesky's ~1 MB cap
            if tmp:
                _tmps.append(tmp)
            import mimetypes
            mime = mimetypes.guess_type(use_path)[0] or "image/jpeg"
            blob = await self.upload_blob(use_path, mime)
            if blob:
                images.append({"alt": alts[i] if i < len(alts) else "", "image": blob})
        for _t in _tmps:
            try:
                import os
                os.remove(_t)
            except OSError:
                pass
        if images:
            record["embed"] = {
                "$type": "app.bsky.embed.images",
                "images": images,
            }

        # Content labels (NSFW self-labelling)
        if labels:
            record["labels"] = {
                "$type": "com.atproto.label.defs#selfLabels",
                "values": [{"val": lbl} for lbl in labels],
            }

        data = {
            "repo": self._did,
            "collection": "app.bsky.feed.post",
            "record": record,
        }

        result = await self._post_json(
            f"{_API_BASE}/com.atproto.repo.createRecord", data
        )
        if result and "uri" in result:
            rkey = result["uri"].rsplit("/", 1)[-1]
            url = f"https://bsky.app/profile/{self._handle}/post/{rkey}"
            logger.info("BSKY: Created post %s — %s", result["uri"][:50], url)
            result["url"] = url
            return result
        logger.error("BSKY: Post creation failed: %s", result)
        return None

    async def delete_post(self, uri: str) -> bool:
        """Delete a post by AT URI."""
        if not await self.ensure_logged_in():
            return False
        rkey = uri.rsplit("/", 1)[-1]
        result = await self._post_json(
            f"{_API_BASE}/com.atproto.repo.deleteRecord",
            {
                "repo": self._did,
                "collection": "app.bsky.feed.post",
                "rkey": rkey,
            },
        )
        if result is not None:
            logger.info("BSKY: Deleted post %s", uri[:50])
            return True
        return False

    async def resolve_handle(self, handle: str) -> str | None:
        """Resolve a Bluesky handle to its DID (needed to build a mention facet)."""
        handle = (handle or "").lstrip("@").strip()
        if not handle:
            return None
        data = await self._get_json(
            f"{_API_BASE}/com.atproto.identity.resolveHandle", params={"handle": handle})
        if isinstance(data, dict) and data.get("did"):
            return data["did"]
        logger.warning("BSKY: could not resolve handle @%s for a mention facet", handle)
        return None

    async def _build_facets(self, text: str,
                            mention_handles: list[str] | None = None) -> list[dict]:
        """Assemble link + mention + hashtag facets, non-overlapping, byte-sorted.

        Mentions cover both the explicitly-bound handles (``mention_handles``,
        from handle-book contacts) AND any full ``@handle.tld`` typed directly in
        the text. A later facet that would overlap an earlier one (e.g. a
        ``#anchor`` or an ``@handle`` sitting inside a URL) is dropped, since AT
        Protocol rejects overlapping facet ranges.
        """
        def _overlaps(s: int, e: int, ranges: list[tuple]) -> bool:
            return any(not (e <= os or s >= oe) for os, oe in ranges)

        facets = list(self._extract_link_facets(text))   # URLs first
        occupied = [(f["index"]["byteStart"], f["index"]["byteEnd"]) for f in facets]

        handles = list(mention_handles or []) + self._detect_handle_mentions(text)
        for mf in await self._build_mention_facets(text, handles):
            s, e = mf["index"]["byteStart"], mf["index"]["byteEnd"]
            if _overlaps(s, e, occupied):
                continue
            facets.append(mf)
            occupied.append((s, e))

        for tf in self._extract_tag_facets(text):
            s, e = tf["index"]["byteStart"], tf["index"]["byteEnd"]
            if _overlaps(s, e, occupied):
                continue
            facets.append(tf)
            occupied.append((s, e))

        facets.sort(key=lambda f: f["index"]["byteStart"])
        return facets

    @staticmethod
    def _detect_handle_mentions(text: str) -> list[str]:
        """Full ``@handle.tld`` strings typed directly in the text — domain-like,
        so a bare ``@alias`` (which needs a handle-book binding) is ignored, as is
        an email's ``@domain``. Returned without the leading @; deduped/resolved
        by ``_build_mention_facets``."""
        import re as _re
        out = []
        for m in _re.finditer(
                r'(?<![\w@.])@([A-Za-z0-9][A-Za-z0-9-]*(?:\.[A-Za-z0-9-]+)+)', text):
            h = m.group(1).rstrip('.')
            if h:
                out.append(h)
        return out

    async def _build_mention_facets(self, text: str, handles: list[str]) -> list[dict]:
        """Facets for each ``@handle`` in text whose handle resolves to a DID."""
        facets: list[dict] = []
        seen: set[str] = set()
        for h in handles:
            h = (h or "").lstrip("@").strip()
            if not h or h in seen:
                continue
            seen.add(h)
            did = await self.resolve_handle(h)
            if not did:
                continue
            needle = "@" + h
            start = 0
            while True:
                idx = text.find(needle, start)
                if idx < 0:
                    break
                start_bytes = len(text[:idx].encode("utf-8"))
                end_bytes = start_bytes + len(needle.encode("utf-8"))
                facets.append({
                    "index": {"byteStart": start_bytes, "byteEnd": end_bytes},
                    "features": [{"$type": "app.bsky.richtext.facet#mention", "did": did}],
                })
                start = idx + len(needle)
        return facets

    @staticmethod
    def _extract_link_facets(text: str) -> list[dict]:
        """Extract URL facets from post text for AT Protocol rich text."""
        import re as _re
        url_pattern = _re.compile(r'https?://\S+')
        facets = []
        for m in url_pattern.finditer(text):
            url = m.group(0)
            # Calculate byte offsets
            start_bytes = len(text[:m.start()].encode("utf-8"))
            end_bytes = start_bytes + len(url.encode("utf-8"))
            facets.append({
                "index": {"byteStart": start_bytes, "byteEnd": end_bytes},
                "features": [{"$type": "app.bsky.richtext.facet#link", "uri": url}],
            })
        return facets

    @staticmethod
    def _extract_tag_facets(text: str) -> list[dict]:
        """Extract #hashtag facets. A tag starts with a letter (so ``#1`` and a
        bare ``#`` are ignored) and runs to the next whitespace/#; the facet's
        ``tag`` value excludes the leading ``#``."""
        import re as _re
        facets = []
        for m in _re.finditer(r'#([A-Za-z][^\s#]*)', text):
            tag_text = m.group(0)      # includes '#'
            tag = m.group(1).rstrip('.,!?;:)\'"')   # trim trailing punctuation
            if not tag:
                continue
            start = m.start()
            start_bytes = len(text[:start].encode("utf-8"))
            end_bytes = start_bytes + len(("#" + tag).encode("utf-8"))
            facets.append({
                "index": {"byteStart": start_bytes, "byteEnd": end_bytes},
                "features": [{"$type": "app.bsky.richtext.facet#tag", "tag": tag}],
            })
        return facets
