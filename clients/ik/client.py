"""Itaku (IK) API client.

Itaku provides a public REST API at itaku.ee/api/. No authentication
is required — only the target username is needed. Content is split into
two types: images (``galleries/images``) and posts (``posts``); the paths
live in ``_PATHS`` because they have moved once already.

Key details:
  - Content IDs are integers
  - Stats: likes (num_likes), comments (num_comments), reshares (num_reshares)
  - NO views metric available
  - Auth: none required (public API)
  - Pagination: cursor-based (next URL in response)
  - Content types: images and posts
"""

from __future__ import annotations
import asyncio
import logging
from typing import Any

import httpx

import config

logger = logging.getLogger(__name__)
_API_BASE = "https://itaku.ee/api"
# Itaku rejects an image carrying fewer than this; its own dialog says so.
_MIN_IMAGE_TAGS = 5
_WEB_BASE = "https://itaku.ee"

# ⚠ Itaku moved its gallery endpoints and this client did not notice for
# 1,955 polls. `/api/gallery_images/` — list AND detail — now returns 404;
# both live under `/api/galleries/images/`. Nothing surfaced it because
# `_get_json` maps a 404 to None, `_paginate_content` read that as "no more
# pages", and the poller logged `status='success', submissions_found=0`. A
# check that cannot fail: it could not tell "this account has no art" from
# "we asked the wrong URL".
#
# Measured 2026-08-27 against the live API:
#   /api/gallery_images/            -> 404
#   /api/galleries/images/?owner=N  -> 200, correctly filtered to owner N
#   /api/gallery_images/{id}/       -> 404
#   /api/galleries/images/{id}/     -> 200
#
# ⚠ The correct path was ALREADY in this file. `upload_image` has posted to
# `{_API_BASE}/galleries/images/` all along — so the move was noticed once, on
# the write side, and the read side was never updated. One fact, two
# declarations, no check, both declarations 130 lines apart in the same module.
# Declared here so a future move is one edit, not three.
_PATHS = {"image": "galleries/images", "post": "posts"}

_HEADERS = {
    "User-Agent": "PawPoller/1.0",
    "Accept": "application/json",
}


def _auth_header(token: str) -> dict:
    """Build Itaku's auth header from a token that may have been pasted loosely.

    Itaku is Django REST Framework, whose ``TokenAuthentication`` splits the
    header on whitespace and rejects anything that is not exactly two parts with
    **"Token string should not contain spaces."** That is a 401 whose text
    blames the token while the real fault is the header shape, and it is what
    prod returned on 2026-08-19 while posting ``Showing_Off``.

    Two ways a copied token produces it, both of them the operator doing
    something reasonable:

    * pasting ``Token abc123`` straight out of a DevTools request header, which
      makes the header ``Token Token abc123`` — three parts;
    * a trailing newline or space picked up by the copy, which makes it three
      parts once DRF splits.

    Neither is worth a support round trip, so the token is normalised here
    rather than trusted. Stripping is safe: a DRF token key is hex and never
    contains whitespace, so anything trimmed was never part of it.
    """
    clean = (token or "").strip()
    if clean.lower().startswith("token "):
        clean = clean[6:].strip()
    return {"Authorization": f"Token {clean}"}


class IKClient:
    """Async HTTP client for Itaku's public API."""

    def __init__(self, target_user: str, proxy_url: str = "", proxy_key: str = ""):
        self.target_user = target_user
        self._user_id: int | None = None
        if proxy_url and proxy_key:
            from polling.cf_proxy import CloudflareProxyTransport
            transport = CloudflareProxyTransport(proxy_url, proxy_key)
            logger.info("IK client using CF proxy: %s", proxy_url)
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
        self._user_id = None  # Reset cached user ID

    async def close(self) -> None:
        await self._http.aclose()

    async def _get_json(self, url: str, params: dict | None = None) -> dict | list | None:
        """Fetch a JSON endpoint with error handling."""
        try:
            resp = await self._http.get(url, params=params)
            if resp.status_code == 404:
                logger.warning("IK: Not found (404) for %s", url)
                return None
            if resp.status_code == 429:
                logger.warning("IK: Rate limited (429), waiting 30s...")
                await asyncio.sleep(30)
                resp = await self._http.get(url, params=params)
            resp.raise_for_status()
            return resp.json()
        except httpx.HTTPError as e:
            logger.error("IK: Failed to fetch %s: %s", url, e)
            return None
        except Exception as e:
            logger.error("IK: JSON parse error for %s: %s", url, e)
            return None

    # ── User Resolution ───────────────────────────────────────

    async def _resolve_user_id(self) -> int | None:
        """Resolve username to user ID via the profile endpoint."""
        if self._user_id is not None:
            return self._user_id

        data = await self._get_json(f"{_API_BASE}/user_profiles/{self.target_user}/")
        if data and isinstance(data, dict):
            self._user_id = data.get("owner")
            return self._user_id
        return None

    async def validate_user(self) -> str | None:
        """Check if the target user exists on Itaku. Returns username if valid."""
        if not self.target_user:
            return None
        data = await self._get_json(f"{_API_BASE}/user_profiles/{self.target_user}/")
        if data and isinstance(data, dict) and data.get("owner"):
            self._user_id = data["owner"]
            return self.target_user
        return None

    async def validate_token(self, token: str) -> dict:
        """Check an auth token against Itaku and say precisely what is wrong.

        Worth doing at save time rather than at post time. The token is not
        needed for tracking, so a wrong one sits in settings looking fine until
        the next upload fails — which is what happened on 2026-08-19: the first
        anyone knew was a 401 in the middle of posting ``Showing_Off``.

        The three outcomes are distinguishable and mean different things, so
        they are reported separately rather than collapsed into a bool:

        ``ok``            the token authenticates; ``username`` is who as
        ``invalid``       Itaku says "Invalid token" — wrong or revoked value
        ``malformed``     the *header* was rejected, not the token. Itaku is
                          Django REST Framework, which splits the auth header on
                          whitespace and refuses anything that is not two parts.
                          ``_auth_header`` normalises the two ways that happens,
                          so seeing this now means something stranger.

        ``GET /api/auth/user/`` is the check: DRF's identity endpoint, cheap,
        read-only, and it exists precisely to answer "who is this token".
        """
        token = (token or "").strip()
        if not token:
            return {"status": "invalid", "detail": "No token given."}
        try:
            resp = await self._http.get(f"{_API_BASE}/auth/user/", headers=_auth_header(token))
        except httpx.HTTPError as e:
            # A network failure is not a bad token, and saying so would send
            # the user hunting for a new one.
            return {"status": "error", "detail": f"Could not reach Itaku: {e}"}

        if resp.status_code == 200:
            data = {}
            try:
                data = resp.json()
            except Exception:
                pass
            username = ""
            if isinstance(data, dict):
                username = (data.get("username") or data.get("owner_username")
                            or (data.get("profile") or {}).get("username") or "")
            return {"status": "ok", "username": username}

        detail = ""
        try:
            detail = (resp.json() or {}).get("detail", "")
        except Exception:
            detail = resp.text[:200]
        if "should not contain spaces" in detail or "Invalid token header" in detail:
            return {"status": "malformed", "detail": detail}
        return {"status": "invalid", "detail": detail or f"HTTP {resp.status_code}"}

    async def get_follower_count(self) -> int | None:
        """Best-effort follower count from the Itaku user profile.

        Itaku's /user_profiles/{name}/ payload is the only profile source the
        client already uses; the follower field name isn't formally documented,
        so try the plausible keys and return None if none are present (the
        follower series simply won't populate for Itaku rather than storing junk).
        """
        if not self.target_user:
            return None
        data = await self._get_json(f"{_API_BASE}/user_profiles/{self.target_user}/")
        if not data or not isinstance(data, dict):
            return None
        for key in ("num_followers", "followers_count", "follower_count", "num_follower"):
            val = data.get(key)
            if val is not None:
                try:
                    return int(val)
                except (TypeError, ValueError):
                    return None
        return None

    # ── Content Discovery ─────────────────────────────────────

    async def get_all_content_ids(self) -> list[dict]:
        """Fetch all images and posts for the target user."""
        user_id = await self._resolve_user_id()
        if not user_id:
            logger.error("IK: Could not resolve user ID for %s", self.target_user)
            return []

        all_content: list[dict] = []

        images = await self._paginate_content(_PATHS["image"], user_id, "image")
        posts = await self._paginate_content(_PATHS["post"], user_id, "post")
        if images is None or posts is None:
            # `or`, not `and`: one dead endpoint is enough. Returning [] here is
            # exactly what let a 404 read as an empty gallery for 1,955 polls,
            # so fail loudly and let the poller log an error. The cost of being
            # strict is a noisy failure if Itaku retires one content type; the
            # cost of being lenient is silence, which is what happened.
            dead = ", ".join(k for k, v in (("images", images), ("posts", posts))
                             if v is None)
            raise RuntimeError(
                f"IK: the {dead} endpoint returned nothing readable for "
                f"{self.target_user} — check whether the paths in _PATHS "
                f"have moved again (they did once already)")
        all_content.extend(images or [])
        all_content.extend(posts or [])

        logger.info("IK: Found %d content items (%d images, %d posts) for %s",
                    len(all_content), len(images or []), len(posts or []),
                    self.target_user)
        return all_content

    async def _paginate_content(self, path: str, user_id: int,
                                kind: str) -> list[dict] | None:
        """One content type, every page.

        Returns ``None`` when the endpoint itself failed — distinct from ``[]``,
        which means the account genuinely has none of this content type. That
        distinction is the whole point: collapsing the two is what let a 404
        masquerade as an empty gallery.

        ``owner`` must be the numeric user id. ``owner__username`` is **silently
        ignored** by this API — it returns the site-wide feed rather than an
        error — so a wrong filter here would file strangers' art under this
        account. The owner check below is the guard against that.
        """
        items: list[dict] = []
        url = f"{_API_BASE}/{path}/"
        params = {"owner": str(user_id), "page_size": "30", "ordering": "-date_added"}
        foreign = 0
        first = True

        for _page_safety in range(1000):
            if not url:
                break
            data = await self._get_json(url, params=params)
            if not data or not isinstance(data, dict):
                # A failure on the FIRST request is an endpoint problem; a
                # failure part-way through is a truncated read of real data.
                return None if first else items
            first = False

            results = data.get("results", [])
            if not results:
                break

            for item in results:
                item_id = item.get("id")
                if not item_id:
                    continue
                # Never store content we cannot prove belongs to this account.
                # The API accepts an unknown filter by ignoring it, so a typo or
                # a renamed parameter hands back the public firehose looking
                # exactly like a gallery.
                owner = item.get("owner")
                if owner is not None and int(owner) != int(user_id):
                    foreign += 1
                    continue
                items.append({
                    "content_id": int(item_id),
                    "title": item.get("title", ""),
                    "content_type": kind,
                })

            # Cursor pagination. `next` used to sit at the top level and now
            # lives under `links`; reading only the old place stopped every
            # fetch after page one. Both are accepted.
            next_url = (data.get("links") or {}).get("next") or data.get("next")
            if next_url:
                url = next_url
                params = None  # params are already in the next URL
            else:
                break

            await asyncio.sleep(config.IK_REQUEST_DELAY_SECONDS)

        if foreign:
            logger.warning(
                "IK: dropped %d %s item(s) not owned by user %s — the owner filter "
                "is being ignored, which means the endpoint or its parameters have "
                "changed", foreign, kind, user_id)
        return items

    # ── Content Details ───────────────────────────────────────

    async def get_content_detail(self, content_id: int, content_type: str = "image") -> dict:
        """Fetch stats and metadata for a single content item."""
        endpoint = _PATHS.get(content_type, _PATHS["post"])
        data = await self._get_json(f"{_API_BASE}/{endpoint}/{content_id}/")

        if not data or not isinstance(data, dict):
            return {
                "content_id": content_id, "title": "", "username": self.target_user,
                "likes": 0, "comments_count": 0, "reshares": 0,
                "keywords": [], "link": f"{_WEB_BASE}/{self.target_user}/gallery/{content_id}",
                "description": "", "content_type": content_type, "posted_at": "",
            }

        detail = {
            "content_id": int(data.get("id", content_id)),
            "title": data.get("title", ""),
            "username": self.target_user,
            "description": data.get("description", "") or "",
            "content_type": content_type,
            "likes": data.get("num_likes", 0),
            "comments_count": data.get("num_comments", 0),
            "reshares": data.get("num_reshares", 0),
            "posted_at": data.get("date_added", ""),
            "link": f"{_WEB_BASE}/{self.target_user}/gallery/{content_id}" if content_type == "image" else f"{_WEB_BASE}/{self.target_user}/posts/{content_id}",
            "maturity_rating": data.get("maturity_rating", ""),
        }

        # Tags/keywords
        tags = data.get("tags", [])
        if isinstance(tags, list):
            detail["keywords"] = [t.get("name", str(t)) if isinstance(t, dict) else str(t) for t in tags]
        else:
            detail["keywords"] = []

        # Thumbnail
        if content_type == "image":
            detail["thumbnail_url"] = data.get("image_sm", data.get("image", ""))
        else:
            detail["thumbnail_url"] = ""

        # Rating mapping
        mr = data.get("maturity_rating", "")
        if mr == "SFW" or mr == "0" or mr == 0:
            detail["rating"] = "General"
        elif mr == "Questionable" or mr == "1" or mr == 1:
            detail["rating"] = "Mature"
        elif mr == "NSFW" or mr == "2" or mr == 2:
            detail["rating"] = "Adult"
        else:
            detail["rating"] = str(mr) if mr else "General"

        return detail

    async def get_content_details_batch(self, content_items: list[dict]) -> list[dict]:
        """Fetch details for multiple content items with rate limiting."""
        details = []
        for i, item in enumerate(content_items):
            if i > 0:
                await asyncio.sleep(config.IK_REQUEST_DELAY_SECONDS)
            try:
                detail = await self.get_content_detail(
                    item["content_id"],
                    item.get("content_type", "image"),
                )
                details.append(detail)
            except Exception as e:
                logger.warning("IK: Failed to fetch content %s: %s", item.get("content_id"), e)
        return details

    # ── Posting / Upload ────────────────────────────────────────

    async def upload_image(
        self,
        file_path: str,
        *,
        title: str = "",
        description: str = "",
        tags: list[str] | None = None,
        maturity_rating: str = "SFW",
        visibility: str = "PUBLIC",
        sections: list[int] | None = None,
        share_on_feed: bool = True,
        token: str = "",
    ) -> dict:
        """Upload an image to Itaku gallery.

        Args:
            file_path: Path to image file (PNG, JPG, GIF, WEBP).
            title: Image title.
            description: Plaintext description (max 5000 chars).
            tags: List of tag names (min 5 tags).
            maturity_rating: "SFW", "Questionable", or "NSFW".
            visibility: "PUBLIC", "FOLLOWERS_ONLY", or "PRIVATE".
            sections: Gallery folder IDs (optional).
            share_on_feed: Post to activity feed.
            token: Auth token (from browser session).

        Returns:
            Dict with 'id' and 'url'.
        """
        import os

        if not token:
            raise RuntimeError("Itaku auth token required for uploads")

        with open(file_path, "rb") as f:
            file_data = f.read()

        filename = os.path.basename(file_path)
        tag_json = [{"name": t} for t in (tags or [])]

        # Build multipart form
        import json
        files = {"image": (filename, file_data)}
        data = {
            "title": title,
            "description": description[:5000],
            "tags": json.dumps(tag_json),
            "maturity_rating": maturity_rating,
            "visibility": visibility,
            "share_on_feed": "true" if share_on_feed else "false",
        }
        if sections:
            data["sections"] = json.dumps(sections)

        resp = await self._http.post(
            f"{_API_BASE}/galleries/images/",
            data=data,
            files=files,
            headers=_auth_header(token),
            timeout=60.0,
        )

        if resp.status_code == 429:
            logger.warning("IK: Rate limited on upload, waiting 30s...")
            await asyncio.sleep(30)
            resp = await self._http.post(
                f"{_API_BASE}/galleries/images/",
                data=data,
                files=files,
                headers=_auth_header(token),
                timeout=60.0,
            )

        if resp.status_code not in (200, 201):
            raise RuntimeError(f"IK: Upload failed — status {resp.status_code}: {resp.text[:200]}")

        result = resp.json()
        image_id = result.get("id", "")
        logger.info("IK: Uploaded image %s — %s", image_id, title[:40])
        return {"id": str(image_id), "url": f"{_WEB_BASE}/image/{image_id}"}


    async def edit_image(
        self,
        image_id: str | int,
        *,
        title: str | None = None,
        description: str | None = None,
        tags: list[str] | None = None,
        maturity_rating: str | None = None,
        visibility: str | None = None,
        sections: list[int] | None = None,
        token: str = "",
    ) -> dict:
        """Edit an existing gallery image's metadata.

        ``PATCH /api/galleries/images/{id}/`` — the DRF sibling of the
        ``POST /api/galleries/images/`` used by :meth:`upload_image`, taking the
        same field names, and the same token auth. Itaku's own web client drives
        exactly these fields from its "Edit image" dialog
        (``app-edit-image-dialog``): title, description, tags, folders
        (``sections``), visibility, and the SFW/Questionable/NSFW maturity toggle.

        Only the fields passed are sent — DRF's PATCH is a partial update, so
        anything left as None keeps whatever is on the image.

        ⚠ **``share_on_feed`` is deliberately never sent.** The upload path sets
        it, and Itaku reads it as "announce this to my followers". Sending it on
        an edit would push a piece back onto the activity feed of everyone
        following the account **every time metadata is synced** — a silent spam
        cannon on a routine background operation.

        ⚠ Itaku requires **at least 5 tags** on an image (its own dialog says so).
        Sending fewer is rejected by the API, so callers must not thin the list.

        Returns ``{"id", "url"}``. Raises ``RuntimeError`` carrying Itaku's own
        message when the edit is rejected.
        """
        if not token:
            raise RuntimeError("Itaku auth token required for edits")

        import json

        data: dict[str, Any] = {}
        if title is not None:
            # The dialog's own counter caps the field at 100.
            data["title"] = title[:100]
        if description is not None:
            data["description"] = description[:5000]
        if tags is not None:
            clean = [t for t in (str(x).strip() for x in tags) if t]
            if len(clean) < _MIN_IMAGE_TAGS:
                raise RuntimeError(
                    f"Itaku requires at least {_MIN_IMAGE_TAGS} tags on an image "
                    f"(got {len(clean)}) — refusing to send a set it will reject"
                )
            data["tags"] = json.dumps([{"name": t} for t in clean])
        if maturity_rating is not None:
            if maturity_rating not in ("SFW", "Questionable", "NSFW"):
                raise RuntimeError(
                    f"Itaku maturity_rating must be SFW/Questionable/NSFW "
                    f"(got {maturity_rating!r})"
                )
            data["maturity_rating"] = maturity_rating
        if visibility is not None:
            data["visibility"] = visibility
        if sections is not None:
            data["sections"] = json.dumps(sections)

        if not data:
            return {"id": str(image_id), "unchanged": True,
                    "url": f"{_WEB_BASE}/image/{image_id}"}

        url = f"{_API_BASE}/galleries/images/{image_id}/"
        resp = await self._http.patch(
            url, data=data, headers=_auth_header(token), timeout=60.0,
        )
        if resp.status_code == 429:
            logger.warning("IK: Rate limited on edit, waiting 30s...")
            await asyncio.sleep(30)
            resp = await self._http.patch(
                url, data=data, headers=_auth_header(token), timeout=60.0,
            )

        if resp.status_code not in (200, 201, 202):
            raise RuntimeError(
                f"IK: Edit failed — status {resp.status_code}: {resp.text[:200]}"
            )

        logger.info("IK: Edited image %s (%d fields)", image_id, len(data))
        return {"id": str(image_id), "url": f"{_WEB_BASE}/image/{image_id}"}

    async def create_post(
        self,
        *,
        title: str = "",
        content: str = "",
        tags: list[str] | None = None,
        maturity_rating: str = "SFW",
        visibility: str = "PUBLIC",
        gallery_images: list[int] | None = None,
        token: str = "",
    ) -> dict:
        """Create a text post on Itaku.

        Posts are text/blog-style content. Can optionally reference gallery images.
        Content is plaintext, max ~5000 chars.

        Args:
            title: Post title.
            content: Post body text (plaintext).
            tags: List of tag names.
            maturity_rating: "SFW", "Questionable", or "NSFW".
            visibility: "PUBLIC", "FOLLOWERS_ONLY", or "PRIVATE".
            gallery_images: List of gallery image IDs to attach.
            token: Auth token.

        Returns:
            Dict with 'id' and 'url'.
        """
        if not token:
            raise RuntimeError("Itaku auth token required for posting")

        import json
        tag_json = [{"name": t} for t in (tags or [])]
        payload = {
            "title": title,
            "content": content[:5000],
            "tags": tag_json,
            "maturity_rating": maturity_rating,
            "visibility": visibility,
            "gallery_images": gallery_images or [],
        }

        resp = await self._http.post(
            f"{_API_BASE}/posts/",
            json=payload,
            headers=_auth_header(token),
            timeout=30.0,
        )

        if resp.status_code not in (200, 201):
            raise RuntimeError(f"IK: Post creation failed — status {resp.status_code}: {resp.text[:200]}")

        result = resp.json()
        post_id = result.get("id", "")
        logger.info("IK: Created post %s — %s", post_id, title[:40])
        return {"id": str(post_id), "url": f"{_WEB_BASE}/post/{post_id}"}
