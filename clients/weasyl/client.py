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



# Weasyl's own category codes (its submit forms, read 2026-10-02). It refuses anything else —
# 0 included, which is what an unset category used to send.
VISUAL_SUBTYPES = {1010, 1020, 1030, 1040, 1050, 1060, 1070, 1075, 1078, 1080, 1999}
LITERARY_SUBTYPES = {2010, 2020, 2030, 2999}


def _subtype(value, allowed: set, default: int) -> int:
    try:
        v = int(value or 0)
    except (TypeError, ValueError):
        return default
    return v if v in allowed else default


def _submitted_id(final_url: str, text: str) -> str:
    """The new submission's id from where Weasyl sent us: the submission page (today
    ``/~user/submissions/N/slug``, once ``/submission/N``), or the thumbnail step
    (``/manage/thumbnail?submitid=N``), or a link in the page."""
    m = (re.search(r'/submissions?/(\d+)', final_url) or re.search(r'[?&]submitid=(\d+)', final_url)
         or re.search(r'/submissions?/(\d+)', text[:2000]))
    return m.group(1) if m else ""


def _submission_url(final_url: str, submission_id: str) -> str:
    """Weasyl's own address when it landed on the submission page, else the short form."""
    return final_url if re.search(r"/submissions?/\d+", final_url) else f"https://www.weasyl.com/submission/{submission_id}"


def _refusal(text: str) -> str:
    """Weasyl's own words when it refuses a submission (it re-renders a page with 200)."""
    m = (re.search(r'<[^>]+(?:id|class)="[^"]*error[^"]*"[^>]*>(.*?)</(?:div|p|section)>', text, re.S | re.I)
         or re.search(r'<title>(.*?)</title>', text, re.S | re.I))
    if not m:
        return ""
    # `<[^>]*>?` also eats an unclosed tag at the end (WSREFUSAL, 4.56.2); any stray < > left go too.
    words = re.sub(r"\s+", " ", re.sub(r"[<>]", " ", re.sub(r"<[^>]*>?", " ", m.group(1))))
    return re.sub(r" ([.,!?;:])", r"\1", words).strip()[:200]


def _journal_refusal(text: str, status: int) -> str:
    """Weasyl's reason for refusing a journal; its unverified-account code named in plain words."""
    if "vouchRequired" in text or "has to be verified" in text:
        return ("Weasyl needs your account verified (\"vouched\") before it takes journals — the same check its "
                "submissions need")
    return f"Weasyl refused the journal: {_refusal(text) or 'no reason given'} (status {status})"


def _image_mime(path: str) -> str:
    """MIME for a cover / thumbnail upload by extension (4.19.3)."""
    ext = os.path.splitext(path or "")[1].lower()
    return {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".gif": "image/gif", ".webp": "image/webp"}.get(ext, "image/png")

class WeasylClient:
    """Weasyl REST API client using API key authentication."""

    def __init__(self, api_key: str = "", proxy_url: str = "", proxy_key: str = ""):
        self.api_key = api_key
        # Weasyl authenticates via a custom header: X-Weasyl-API-Key.
        # Unlike OAuth bearer tokens, this key is a static secret generated
        # from the user's account settings. It is sent on every request as
        # a default header on the httpx client so callers don't need to
        # manage auth per-request.
        # Kept so the plain (key-less) client for the cover image routes the same way
        # (WSPLAINPROXY, 4.56.2 release review).
        self._proxy = (proxy_url, proxy_key) if proxy_url and proxy_key else None
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
            "subtype": str(_subtype(subtype, LITERARY_SUBTYPES, 2010)),   # unset -> Story
        }
        if csrf:
            form_data["token"] = csrf
        if folder_id:
            form_data["folderid"] = str(folder_id)

        files = {"submitfile": (filename, file_data)}
        if cover_path and os.path.isfile(cover_path):
            # BOTH slots (4.54.3): Weasyl shows coverfile on the page but makes no gallery
            # thumbnail from it — the first live story got the default placeholder.
            with open(cover_path, "rb") as cf:
                cover = cf.read()
            mime = _image_mime(cover_path)
            files["coverfile"] = (os.path.basename(cover_path), cover, mime)
            files["thumbfile"] = (os.path.basename(cover_path), cover, mime)

        # Use a client that follows redirects for this request
        resp = await self._http.post(
            "https://www.weasyl.com/submit/literary",
            data=form_data,
            files=files,
            timeout=60.0,
            follow_redirects=True,
        )

        final_url = str(resp.url)
        submission_id = _submitted_id(final_url, resp.text)
        if submission_id:
            logger.info("WS: Submitted literary work — id=%s", submission_id)
            if cover_path and os.path.isfile(cover_path):
                try:
                    await self.thumbnail_from_cover(submission_id)
                except Exception as e:   # the post itself succeeded
                    logger.warning("WS: thumbnail crop failed for %s: %s", submission_id, e)
            return {"submission_id": submission_id, "url": _submission_url(final_url, submission_id)}
        raise RuntimeError(f"Weasyl refused the story: {_refusal(resp.text) or 'no reason given'} "
                           f"(status {resp.status_code})")

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
            "subtype": str(_subtype(subtype, VISUAL_SUBTYPES, 1030)),     # unset -> Digital
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
        submission_id = _submitted_id(final_url, resp.text)
        if submission_id:
            logger.info("WS: Submitted visual work — id=%s", submission_id)
            return {"submission_id": submission_id, "url": _submission_url(final_url, submission_id)}
        raise RuntimeError(f"Weasyl refused the artwork: {_refusal(resp.text) or 'no reason given'} "
                           f"(status {resp.status_code})")

    async def submit_multimedia(
        self,
        file_path: str,
        *,
        title: str = "",
        description: str = "",
        tags: str = "",
        rating: int = 40,
        subtype: int = 3010,
        folder_id: int | None = None,
        cover_path: str | None = None,
    ) -> dict:
        """Submit a multimedia work (mp3) to Weasyl — MEDIATYPES phase 2, 4.19.3.

        ``/submit/multimedia`` is the third submit form beside visual and
        literary, with the same fields: ``submitfile`` is the audio, and the
        poster goes as BOTH ``coverfile`` (shown on the submission page) and
        ``thumbfile`` (the gallery thumbnail), which is what PostyBirb sends for
        an audio file. ``subtype`` is Weasyl's multimedia subtype code:
        3010 Original Music, 3020 Cover Version, 3030 Remix / Mashup, 3040
        Speech / Reading, 3999 Other.
        """
        csrf = await self._get_csrf_token("https://www.weasyl.com/submit/multimedia")

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

        files = {"submitfile": (filename, file_data, "audio/mpeg")}
        if cover_path and os.path.isfile(cover_path):
            with open(cover_path, "rb") as cf:
                cover = cf.read()
            mime = _image_mime(cover_path)
            files["coverfile"] = (os.path.basename(cover_path), cover, mime)
            files["thumbfile"] = (os.path.basename(cover_path), cover, mime)

        resp = await self._http.post(
            "https://www.weasyl.com/submit/multimedia",
            data=form_data,
            files=files,
            timeout=600.0,
            follow_redirects=True,
        )

        final_url = str(resp.url)
        submission_id = _submitted_id(final_url, resp.text)
        if submission_id:
            logger.info("WS: Submitted multimedia work — id=%s", submission_id)
            if cover_path and os.path.isfile(cover_path):
                try:
                    await self.thumbnail_from_cover(submission_id)
                except Exception as e:   # the post itself succeeded
                    logger.warning("WS: thumbnail crop failed for %s: %s", submission_id, e)
            return {"submission_id": submission_id, "url": _submission_url(final_url, submission_id)}

        raise RuntimeError(f"Weasyl refused the audio: {_refusal(resp.text) or 'no reason given'} "
                           f"(status {resp.status_code})")

    async def edit_submission(
        self,
        submission_id: str,
        *,
        title: str = "",
        description: str = "",
        tags: str = "",
        rating: int | None = None,
    ) -> dict:
        """Edit an existing Weasyl submission's metadata (4.54.4).

        Weasyl's edit form lives at ``/edit/submission?submitid=N`` (the old
        ``/edit/submission/N`` is a 404 — every edit failed) and needs its
        current values posted back (category, folder, ticks), so they are read
        off the form and only what changed is overlaid. Tags are a separate
        form, ``/submit/tags``.
        """
        changes: dict[str, str] = {}
        if title:
            changes["title"] = title
        if description:
            changes["content"] = description
        if rating is not None:
            changes["rating"] = str(rating)
        await self._post_form(f"/edit/submission?submitid={submission_id}", "/edit/submission", changes)
        if tags:
            await self._post_form(f"/submission/{submission_id}", "/submit/tags",
                                  {"submitid": str(submission_id), "tags": tags})
        logger.info("WS: Edited submission %s — title=%r", submission_id, title[:40])
        return {"submission_id": submission_id, "url": f"https://www.weasyl.com/submission/{submission_id}"}

    async def reupload_file(self, submission_id: str, file_path: str) -> None:
        """Replace the submission's file (the story text, picture or audio)."""
        await self._post_form(f"/reupload/submission?submitid={submission_id}", "/reupload/submission",
                              {"targetid": str(submission_id)}, {"submitfile": _upload(file_path)},
                              unchanged_ok=True)

    async def reupload_cover(self, submission_id: str, image_path: str) -> None:
        """Replace a story's / audio's cover, and make it the gallery thumbnail too (Weasyl
        makes no thumbnail from a cover)."""
        await self._post_form(f"/reupload/cover?submitid={submission_id}", "/reupload/cover",
                              {"submitid": str(submission_id)}, {"coverfile": _upload(image_path)},
                              unchanged_ok=True)
        await self.thumbnail_from_cover(submission_id)

    async def thumbnail_from_cover(self, submission_id: str) -> bool:
        """Make the gallery thumbnail the whole cover (4.54.5).

        ``/manage/thumbnail`` is a crop tool: hidden ``x1,y1,x2,y2`` over the image in
        ``<img id="imageselect">``, in that image's own pixels. All-zero means "generate
        one", which for a story is Weasyl's default picture — and an uploaded
        ``thumbfile`` still waits for a crop. Proved in a signed-in browser 2026-10-03:
        0,0 → 300,300 on a 300×300 cover set the thumbnail. -> True when sent."""
        page_path = f"/manage/thumbnail?submitid={submission_id}"
        r = await self._http.get("https://www.weasyl.com" + page_path, follow_redirects=True)
        img = next((t for t in re.findall(r"<img\b[^>]*>", r.text) if 'id="imageselect"' in t), "")
        src = re.search(r'src="([^"]+)"', img)
        if r.status_code != 200 or not src:
            logger.info("WS: no cover to crop a thumbnail from on %s", submission_id)
            return False
        url = src.group(1)
        url = "https:" + url if url.startswith("//") else (url if url.startswith("http") else "https://www.weasyl.com" + url)
        from io import BytesIO
        from PIL import Image
        # The image is fetched WITHOUT the keyed client (WSKEYHOST, 4.56.2): `src` comes from the
        # page, and the keyed client sends X-Weasyl-API-Key to whatever host it names.
        if not url.startswith("https://"):
            logger.info("WS: cover image on %s isn't https — not fetched", submission_id)
            return False
        # Same route as the keyed client (a CF proxy where Weasyl blocks the IP), and a size cap:
        # only the dimensions are needed, so an oversized or endless body is never buffered whole
        # (WSPLAINPROXY).
        proxy = getattr(self, "_proxy", None)
        if proxy:
            from polling.cf_proxy import CloudflareProxyTransport
            transport = CloudflareProxyTransport(*proxy)
        else:
            transport = httpx.AsyncHTTPTransport(retries=2)
        body = bytearray()
        async with httpx.AsyncClient(timeout=30, follow_redirects=True, transport=transport) as plain:
            async with plain.stream("GET", url) as resp:
                if resp.status_code != 200:
                    logger.info("WS: cover image on %s answered %s", submission_id, resp.status_code)
                    return False
                async for chunk in resp.aiter_bytes():
                    body.extend(chunk)
                    if len(body) > _COVER_FETCH_CAP:
                        logger.warning("WS: cover image on %s is over %d MB — not read",
                                       submission_id, _COVER_FETCH_CAP // (1024 * 1024))
                        return False
        try:
            w, h = Image.open(BytesIO(bytes(body))).size
        except Exception as e:
            logger.info("WS: cover image on %s couldn't be read: %s", submission_id, e)
            return False
        await self._post_form(page_path, "/manage/thumbnail",
                              {"submitid": str(submission_id), "x1": "0", "y1": "0", "x2": str(w), "y2": str(h)})
        return True

    # ── Journals (spec 027) ────────────────────────────────────────────────

    async def submit_journal(self, title: str, content: str, rating: int = 10, tags: str = "") -> dict:
        """POST /submit/journal (Markdown ``content``, ``rating`` 10/30/40, space-separated ``tags``). The API
        key header signs the form (no token field — live check 2026-10-10). → {"id", "url"}."""
        resp = await self._http.post("https://www.weasyl.com/submit/journal",
                                     data={"title": title, "rating": str(rating), "content": content, "tags": tags},
                                     timeout=60.0, follow_redirects=True)
        final = str(resp.url)
        m = re.search(r"/journal/(\d+)", final)
        if not m:
            raise RuntimeError(_journal_refusal(resp.text, resp.status_code))
        logger.info("WS: journal posted — id=%s", m.group(1))
        return {"id": m.group(1), "url": final if "/journal/" in final else f"https://www.weasyl.com/journal/{m.group(1)}"}

    async def edit_journal(self, journal_id: str, title: str, content: str, rating: int, tags: str) -> dict:
        """The edit form (current values posted back) + tags through ``/submit/tags``, which edit doesn't hold."""
        await self._post_form(f"/edit/journal?journalid={journal_id}", "/edit/journal",
                              {"journalid": str(journal_id), "title": title, "content": content, "rating": str(rating)})
        await self._post_form(f"/journal/{journal_id}", "/submit/tags", {"journalid": str(journal_id), "tags": tags})
        return {"id": str(journal_id), "url": f"https://www.weasyl.com/journal/{journal_id}"}

    async def remove_journal(self, journal_id: str) -> None:
        resp = await self._http.post("https://www.weasyl.com/remove/journal", data={"journalid": str(journal_id)},
                                     timeout=30.0, follow_redirects=True)
        if resp.status_code >= 400:
            raise RuntimeError(_journal_refusal(resp.text, resp.status_code))

    async def _post_form(self, page: str, action: str, changes: dict, files: dict | None = None,
                         *, unchanged_ok: bool = False) -> None:
        """GET ``page``, take the ``action`` form's current values, overlay ``changes``, POST it.
        A refusal raises with Weasyl's own words; with ``unchanged_ok`` a "you already
        uploaded this file" refusal counts as done (the file there is already this one)."""
        r = await self._http.get("https://www.weasyl.com" + page, follow_redirects=True)
        if r.status_code != 200:
            raise RuntimeError(f"WS: couldn't open {page.split('?')[0]} — status {r.status_code}")
        data = _form_values(r.text, action)
        data.update(changes)
        resp = await self._http.post("https://www.weasyl.com" + action, data=data, files=files or None,
                                     timeout=120.0, follow_redirects=True)
        if resp.status_code >= 400:
            why = _refusal(resp.text) or "no reason given"
            if unchanged_ok and "already" in why.lower():
                logger.info("WS: %s — unchanged (%s)", action, why)
                return
            raise RuntimeError(f"Weasyl refused {action}: {why} (status {resp.status_code})")


# Weasyl's own cover limit is far below this; anything bigger isn't a cover.
_COVER_FETCH_CAP = 20 * 1024 * 1024


def _upload(path: str) -> tuple:
    with open(path, "rb") as f:
        data = f.read()
    ext = os.path.splitext(path)[1].lower()
    mime = {".md": "text/markdown", ".txt": "text/plain", ".pdf": "application/pdf",
            ".mp3": "audio/mpeg"}.get(ext) or _image_mime(path)
    return (os.path.basename(path), data, mime)


def _form_values(page: str, action: str) -> dict[str, str]:
    """The current values a browser would post for the form whose action is ``action``:
    inputs (ticked boxes only), each select's chosen option, textareas. Files aren't read."""
    import html as _html
    m = re.search(rf'<form[^>]*action="{re.escape(action)}"[^>]*>(.*?)</form>', page, re.S)
    if not m:
        return {}
    body, out = m.group(1), {}
    for tag in re.findall(r"<input\b[^>]*>", body):
        name = re.search(r'name="([^"]+)"', tag)
        if not name:
            continue
        typ = (re.search(r'type="([^"]+)"', tag) or [None, "text"])[1].lower()
        val = re.search(r'value="([^"]*)"', tag)
        if typ in ("file", "submit", "button", "image"):
            continue
        if typ in ("checkbox", "radio"):
            if re.search(r"\bchecked\b", tag):
                out[name.group(1)] = _html.unescape(val.group(1)) if val else "on"
            continue
        out[name.group(1)] = _html.unescape(val.group(1)) if val else ""
    for name, opts in re.findall(r'<select[^>]*name="([^"]+)"[^>]*>(.*?)</select>', body, re.S):
        chosen = (re.search(r'<option\b[^>]*\bselected\b[^>]*>', opts)
                  or re.search(r'<option\b[^>]*>', opts))     # none chosen → the browser sends the first
        if chosen:
            v = re.search(r'value="([^"]*)"', chosen.group(0))
            out[name] = _html.unescape(v.group(1)) if v else ""
    for name, text in re.findall(r'<textarea[^>]*name="([^"]+)"[^>]*>(.*?)</textarea>', body, re.S):
        out[name] = _html.unescape(text)
    return out


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
