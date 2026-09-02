"""Weasyl API client for gallery and submission data.

Weasyl uses a simple API-key-based authentication model (no OAuth, no session
cookies). The key is sent as a custom HTTP header on every request, and the
/api/whoami endpoint is used to validate it and discover the owning username.

Unlike Inkbunny (page-based pagination) and FurAffinity (HTML scraping), Weasyl
provides a clean REST JSON API with cursor-based pagination via a `nextid` field.

Note: The Weasyl API does not expose individual comment text -- only a total
comment count is available on each submission. Because of this, there is no
ws_comments table in the database schema.
"""

from __future__ import annotations
import asyncio
import logging
import os
import re
from typing import Any

import httpx

import config

logger = logging.getLogger(__name__)

WEASYL_API_BASE = "https://www.weasyl.com/api"


class WeasylClient:
    """Weasyl REST API client using API key authentication."""

    def __init__(self, api_key: str = "", proxy_url: str = "", proxy_key: str = ""):
        self.api_key = api_key
        # Weasyl authenticates via a custom header: X-Weasyl-API-Key.
        # Unlike OAuth bearer tokens, this key is a static secret generated
        # from the user's account settings. It is sent on every request as
        # a default header on the httpx client so callers don't need to
        # manage auth per-request.
        if proxy_url and proxy_key:
            from polling.cf_proxy import CloudflareProxyTransport
            transport = CloudflareProxyTransport(proxy_url, proxy_key)
            logger.info("Weasyl client using CF proxy: %s", proxy_url)
        else:
            transport = httpx.AsyncHTTPTransport(retries=2)
        self._http = httpx.AsyncClient(
            timeout=30.0,
            headers={"X-Weasyl-API-Key": self.api_key},
            transport=transport,
        )
        self.username: str = ""

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        await self.close()

    async def close(self) -> None:
        await self._http.aclose()

    # ── Validation ───────────────────────────────────────────

    async def validate_key(self) -> str | None:
        """Validate the API key via /api/whoami. Returns username or None.

        The /api/whoami endpoint is Weasyl's key-validation mechanism. When a
        valid API key is provided in the X-Weasyl-API-Key header, the endpoint
        returns JSON like {"login": "username", "userid": 12345}. If the key
        is invalid or missing, it returns an error status. This is the only
        way to verify credentials and discover the authenticated username,
        which is needed for gallery listing endpoints.
        """
        if not self.api_key:
            return None
        try:
            resp = await self._http.get(f"{WEASYL_API_BASE}/whoami")
            resp.raise_for_status()
            data = resp.json()
            # The "login" field contains the display username; store it for
            # use in gallery listing URL paths.
            self.username = data.get("login", "")
            return self.username if self.username else None
        except Exception as e:
            logger.warning("Weasyl API key validation failed: %s", e)
            return None

    async def get_follower_count(self) -> int | None:
        """Best-effort follower count from /api/users/{login}/view statistics.

        Weasyl's public statistics object exposes follow numbers but the exact
        key ("followed"/"followers"/"follow_count") isn't stable across doc
        versions, so probe the plausible keys and return None if none match
        rather than storing an ambiguous value (deliberately NOT reading
        "follows", which is the *following* count).
        """
        username = self.username or await self.validate_key()
        if not username:
            return None
        try:
            resp = await self._http.get(f"{WEASYL_API_BASE}/users/{username}/view")
            resp.raise_for_status()
            stats = (resp.json() or {}).get("statistics", {}) or {}
        except Exception as e:
            logger.warning("Weasyl: follower fetch failed: %s", e)
            return None
        for key in ("followers", "followed", "follow_count"):
            if stats.get(key) is not None:
                return _safe_int(stats.get(key))
        return None

    # ── Gallery Listing ──────────────────────────────────────

    async def get_all_gallery_ids(self) -> list[dict]:
        """Paginate through all gallery submissions using nextid cursor.

        Weasyl uses cursor-based pagination via a `nextid` field, NOT
        traditional page-number-based pagination:

        - Each response includes a `nextid` integer. This is the submission ID
          that marks where the next page begins (exclusive lower bound).
        - To fetch the next page, pass `nextid=<value>` as a query param.
        - When `nextid` is null/absent, there are no more pages.

        This is more robust than page-based pagination because it is immune to
        items being inserted or deleted between page fetches (no skipped or
        duplicated results). It also avoids the performance cost of OFFSET
        on the server side.
        """
        all_subs: list[dict] = []
        next_id: int | None = None  # None means "start from the beginning"

        for _page_safety in range(1000):
            # Request up to 100 submissions per page (Weasyl's max batch size).
            params: dict[str, Any] = {"count": "100"}
            if next_id is not None:
                # Cursor: fetch submissions older than this ID
                params["nextid"] = str(next_id)

            resp = await self._http.get(
                f"{WEASYL_API_BASE}/users/{self.username}/gallery",
                params=params,
            )
            resp.raise_for_status()
            data = resp.json()

            submissions = data.get("submissions", [])
            if not submissions:
                break

            for item in submissions:
                sub_id = item.get("submitid")
                if sub_id:
                    all_subs.append({
                        "submission_id": int(sub_id),
                        "title": item.get("title", ""),
                        # Thumbnail URL extraction: Weasyl nests media URLs
                        # inside a "media" object with arrays per type.
                        # See _normalize_submission() for the full pattern.
                        "thumbnail_url": (item.get("media", {}).get("thumbnail", [{}])[0].get("url", "")
                                          if item.get("media", {}).get("thumbnail") else ""),
                    })

            # Advance the cursor for the next iteration.
            # If nextid is falsy (None, 0, absent), we've reached the end.
            next_id = data.get("nextid")
            if not next_id:
                break
            # Rate-limit between pages to respect Weasyl's API guidelines.
            await asyncio.sleep(config.WS_REQUEST_DELAY_SECONDS)

        return all_subs

    # ── Submission Detail ────────────────────────────────────

    async def get_submission_detail(self, submission_id: int) -> dict:
        """Fetch full submission details and normalize to DB format."""
        resp = await self._http.get(
            f"{WEASYL_API_BASE}/submissions/{submission_id}/view",
        )
        resp.raise_for_status()
        raw = resp.json()
        return self._normalize_submission(raw, submission_id)

    async def get_submission_details_batch(self, submission_ids: list[int]) -> list[dict]:
        """Fetch details for multiple submissions sequentially with rate limiting.

        Weasyl has no bulk/batch submission endpoint, so each submission must
        be fetched individually. Requests are made sequentially (not concurrent)
        with a configurable delay between them to avoid hitting rate limits.
        Failed fetches are logged and skipped rather than aborting the batch.
        """
        details: list[dict] = []
        for i, sid in enumerate(submission_ids):
            try:
                detail = await self.get_submission_detail(sid)
                details.append(detail)
            except Exception as e:
                logger.warning("Failed to fetch Weasyl submission %d: %s", sid, e)
            # Rate-limit delay between requests, but not after the last one.
            if i < len(submission_ids) - 1:
                await asyncio.sleep(config.WS_REQUEST_DELAY_SECONDS)
        return details

    # ── Normalization ────────────────────────────────────────

    @staticmethod
    def _normalize_submission(raw: dict, submission_id: int) -> dict:
        """Normalize Weasyl API submission JSON to our DB dict format.

        Weasyl's JSON structure differs from Inkbunny and FurAffinity in several
        ways that this method handles:

        - Tags come as a list (may also be a comma-separated string in edge cases)
        - Media URLs are nested inside a "media" object with typed arrays:
              {"media": {"thumbnail": [{"url": "..."}], "submission": [{"url": "..."}]}}
          Each type key maps to an array of media objects. We take the first
          entry's URL from each. "submission" contains the full-resolution file;
          "thumbnail" contains the preview image.
        - The owner field may appear as "owner" or "owner_login" depending on
          the API version/endpoint.
        - Comment count is only a number -- the Weasyl API does NOT provide
          individual comment text, usernames, or threading info. This is why
          there is no ws_comments table in the database.
        """
        tags = raw.get("tags", [])
        if isinstance(tags, str):
            # Defensive: handle comma-separated string format if the API
            # ever returns tags that way instead of as a list.
            tags = [t.strip() for t in tags.split(",") if t.strip()]

        # Media URL extraction from nested JSON structure.
        # Weasyl stores media in a dict of arrays: media.thumbnail[], media.submission[].
        # Each array element is an object with at least a "url" key.
        # We extract only the first (primary) URL from each array.
        media = raw.get("media", {})
        thumbnail_url = ""
        media_url = ""
        if media.get("thumbnail"):
            # First thumbnail variant -- usually the only one.
            thumbnail_url = media["thumbnail"][0].get("url", "")
        if media.get("submission"):
            # Full-resolution submission file URL.
            media_url = media["submission"][0].get("url", "")

        return {
            "submission_id": submission_id,
            "title": raw.get("title", ""),
            # Owner may be under "owner" (display name) or "owner_login" (login name).
            "username": raw.get("owner", raw.get("owner_login", "")),
            "posted_at": raw.get("posted_at", ""),
            # Weasyl uses "subtype" (e.g. "visual", "literary", "multimedia")
            # instead of IB's "type_name" or FA's "category/theme".
            "subtype": raw.get("subtype", ""),
            "rating": raw.get("rating", ""),
            "thumbnail_url": thumbnail_url,
            "media_url": media_url,
            "description": raw.get("description", ""),
            "keywords": tags,
            "link": raw.get("link", f"https://www.weasyl.com/~x/submissions/{submission_id}"),
            # Stats: only counts are available, no individual comment/fave data.
            "views": _safe_int(raw.get("views", 0)),
            "favorites_count": _safe_int(raw.get("favorites", 0)),
            # This is only a count -- the Weasyl API provides no comment text.
            "comments_count": _safe_int(raw.get("comments", 0)),
        }


    # ── Posting / Upload ────────────────────────────────────────

    async def _get_csrf_token(self, url: str) -> str:
        """Fetch a page and extract the CSRF token from hidden form input."""
        resp = await self._http.get(url)
        if resp.status_code != 200:
            raise RuntimeError(f"WS: Failed to load {url} — status {resp.status_code}")
        match = re.search(r'name="token"\s*value="([^"]+)"', resp.text)
        if not match:
            match = re.search(r'name="csrf_token"\s*value="([^"]+)"', resp.text)
        if not match:
            # Weasyl may use API key auth for form posts if the key header is present.
            # Return empty and try without CSRF — the API key may be sufficient.
            logger.warning("WS: No CSRF token found on %s — attempting without it", url)
            return ""
        return match.group(1)

    async def submit_literary(
        self,
        file_path: str,
        *,
        title: str = "",
        description: str = "",
        tags: str = "",
        rating: int = 40,
        subtype: int = 0,
        folder_id: int | None = None,
        cover_path: str | None = None,
    ) -> dict:
        """Submit a literary work (story/text) to Weasyl.

        Fetches the submit page first to extract a CSRF token, then POSTs
        the submission with the token + file + metadata.
        """
        # Step 1: Get CSRF token from the submit page
        csrf = await self._get_csrf_token("https://www.weasyl.com/submit/literary")

        with open(file_path, "rb") as f:
            file_data = f.read()

        filename = os.path.basename(file_path)
        form_data = {
            "title": title,
            "rating": str(rating),
            "content": description,
            "tags": tags,
            "subtype": str(subtype),
        }
        if csrf:
            form_data["token"] = csrf
        if folder_id:
            form_data["folderid"] = str(folder_id)

        files = {"submitfile": (filename, file_data)}
        if cover_path and os.path.isfile(cover_path):
            with open(cover_path, "rb") as cf:
                files["coverfile"] = (os.path.basename(cover_path), cf.read(), "image/png")

        # Use a client that follows redirects for this request
        resp = await self._http.post(
            "https://www.weasyl.com/submit/literary",
            data=form_data,
            files=files,
            timeout=60.0,
            follow_redirects=True,
        )

        final_url = str(resp.url)
        # Check for success: redirected to submission page or got 200 on it
        sid_match = re.search(r'/submission/(\d+)', final_url)
        if sid_match:
            submission_id = sid_match.group(1)
            logger.info("WS: Submitted literary work — id=%s url=%s", submission_id, final_url)
            return {"submission_id": submission_id, "url": final_url}

        # Check response body for submission link (some flows don't redirect)
        body_match = re.search(r'/submission/(\d+)', resp.text[:2000])
        if body_match:
            submission_id = body_match.group(1)
            url = f"https://www.weasyl.com/submission/{submission_id}"
            logger.info("WS: Submitted literary work (from body) — id=%s", submission_id)
            return {"submission_id": submission_id, "url": url}

        raise RuntimeError(f"Weasyl submission failed — status {resp.status_code}, url={final_url}")

    async def submit_visual(
        self,
        file_path: str,
        *,
        title: str = "",
        description: str = "",
        tags: str = "",
        rating: int = 40,
        subtype: int = 0,
        folder_id: int | None = None,
        thumbnail_path: str | None = None,
    ) -> dict:
        """Submit a visual artwork (image) to Weasyl.

        Mirrors submit_literary but posts to /submit/visual with the image as
        ``submitfile`` and an optional ``thumbfile``. ``subtype`` is a Weasyl
        visual subtype code (e.g. 1030=Digital); 0 lets Weasyl pick a default.
        """
        csrf = await self._get_csrf_token("https://www.weasyl.com/submit/visual")

        with open(file_path, "rb") as f:
            file_data = f.read()
        filename = os.path.basename(file_path)

        form_data = {
            "title": title,
            "rating": str(rating),
            "content": description,
            "tags": tags,
            "subtype": str(subtype),
        }
        if csrf:
            form_data["token"] = csrf
        if folder_id:
            form_data["folderid"] = str(folder_id)

        files = {"submitfile": (filename, file_data)}
        if thumbnail_path and os.path.isfile(thumbnail_path):
            with open(thumbnail_path, "rb") as tf:
                files["thumbfile"] = (os.path.basename(thumbnail_path), tf.read(), "image/png")

        resp = await self._http.post(
            "https://www.weasyl.com/submit/visual",
            data=form_data,
            files=files,
            timeout=120.0,
            follow_redirects=True,
        )

        final_url = str(resp.url)
        sid_match = re.search(r'/submission/(\d+)', final_url)
        if sid_match:
            submission_id = sid_match.group(1)
            logger.info("WS: Submitted visual work — id=%s url=%s", submission_id, final_url)
            return {"submission_id": submission_id, "url": final_url}

        body_match = re.search(r'/submission/(\d+)', resp.text[:2000])
        if body_match:
            submission_id = body_match.group(1)
            url = f"https://www.weasyl.com/submission/{submission_id}"
            logger.info("WS: Submitted visual work (from body) — id=%s", submission_id)
            return {"submission_id": submission_id, "url": url}

        raise RuntimeError(f"Weasyl visual submission failed — status {resp.status_code}, url={final_url}")

    async def edit_submission(
        self,
        submission_id: str,
        *,
        title: str = "",
        description: str = "",
        tags: str = "",
        rating: int | None = None,
    ) -> dict:
        """Edit an existing Weasyl submission's metadata.

        Fetches the edit page to get CSRF token and current values, then
        posts the updated fields back.
        """
        edit_url = f"https://www.weasyl.com/edit/submission/{submission_id}"

        # GET edit page for CSRF token
        csrf = await self._get_csrf_token(edit_url)

        form_data: dict[str, str] = {}
        if csrf:
            form_data["token"] = csrf
        if title:
            form_data["title"] = title
        if description:
            form_data["content"] = description
        if tags:
            form_data["tags"] = tags
        if rating is not None:
            form_data["rating"] = str(rating)

        resp = await self._http.post(
            edit_url,
            data=form_data,
            timeout=30.0,
            follow_redirects=True,
        )

        if resp.status_code >= 400:
            raise RuntimeError(f"WS: Edit failed — status {resp.status_code}")

        url = f"https://www.weasyl.com/submission/{submission_id}"
        logger.info("WS: Edited submission %s — title=%r", submission_id, title[:40])
        return {"submission_id": submission_id, "url": url}


def _safe_int(val: Any) -> int:
    """Safely convert a value to int.

    Handles None, string-formatted numbers (possibly with commas like "1,234"),
    and already-numeric values. Returns 0 for anything unparseable.
    """
    if val is None:
        return 0
    try:
        if isinstance(val, str):
            # Strip commas from formatted numbers like "1,234"
            val = val.replace(",", "").strip()
        return int(val)
    except (ValueError, TypeError):
        return 0
