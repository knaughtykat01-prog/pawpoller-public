"""SoFurry client — official API v1 for writes, login-free JSON reads for analytics.

**3.4.0 rewrite.** SoFurry shipped an official public API (``https://api.sofurry.com``,
docs at ``developer.sofurry.com/dev-docs``) authenticated with a **Personal Access
Token**. That replaced the entire reverse-engineered auth stack this client used to
carry: the Laravel ``/login`` form scrape, the OAuth2-PKCE bridge through
``/fe/auth/sofurry`` that minted a Remix ``_session``, the ``X-CSRF-Token`` threading,
the cookie import/export, and the unhandled-2FA dead end. All of it is gone — a PAT
never logs in, so there is no session to establish, refresh, or lose.

Two surfaces, deliberately:

* ``self._api`` → **https://api.sofurry.com** with ``Authorization: Bearer <PAT>``.
  Everything that WRITES, plus the authoritative gallery listing.
* ``self._web`` → **https://sofurry.com**, no auth at all. Everything that READS
  ANALYTICS, because the official API returns **no statistics whatsoever** (verified
  against the prose docs, the OpenAPI schema, and live responses — see
  ``docs/reference/sofurry_beta_api_map.md``). These endpoints serve published works
  anonymously, including Adult ones, which is why dropping the login costs nothing.

Gotchas that cost real debugging (all recorded in the API map):

* ``Accept: application/json`` is **mandatory** on the official API. Without it an
  unauthenticated call 302s to ``sofurry.com/login`` with an HTML body instead of
  returning a JSON error.
* **HTTP status and body ``statusCode`` disagree.** An unsupported method returns
  **HTTP 500** carrying ``{"statusCode": 400}``. Never branch on the HTTP status alone.
* **Content cannot be deleted.** ``DELETE`` is unsupported on both the submission and
  content routes, and Laravel ``_method`` spoofing does not help. Content is therefore
  **replaced in place** via ``update_content``; see its docstring.
* Uploaded files must be **>= 1 KB** (``"The file must be between 1 and 512000
  kilobytes."``), so ``maxFileSizes`` is in KB.
* The API is rate limited to **60 requests/minute** (``x-ratelimit-limit``), which is
  undocumented.
* ``api.sofurry.com`` is **IP-blocked from datacenter ranges** (the GCP VM gets a
  Cloudflare 403), so the proxy transport applies to BOTH surfaces — see ``__init__``.
"""

from __future__ import annotations
import asyncio
import json
import logging
import os
import re
from typing import Any

import httpx

import config

logger = logging.getLogger(__name__)

SOFURRY_BASE = "https://sofurry.com"
SOFURRY_API = f"{SOFURRY_BASE}/api"           # internal (anonymous reads only)
SOFURRY_API_V1 = "https://api.sofurry.com"    # official (PAT)

# Where a user creates a token. Surfaced in the dashboard's Connect panel.
SOFURRY_PAT_URL = f"{SOFURRY_BASE}/settings/pat-create"

# SoFurry rating codes (unchanged by the API migration)
_RATING_MAP = {10: "Clean", 20: "Adult"}
_RATING_REVERSE = {"clean": 0, "mature": 10, "adult": 20}

# The official API takes INT category/type codes on write and echoes ints back on
# read — unlike the internal Remix API, which echoed display strings ("writing",
# "shortstory"). Both directions are mapped so a value from either surface round-trips.
_SF_CATEGORY_STR_TO_INT = {
    "writing": 20, "artwork": 10, "photography": 30,
    "music": 40, "video": 50, "3d": 60, "game": 70,
}
_SF_TYPE_STR_TO_INT = {
    "shortstory": 21, "book": 22, "drawing": 11, "comic": 12,
    "animation": 13, "photograph": 31, "track": 41, "album": 42,
}

# SoFurry's documented content-file floor. Enforced server-side with a 422; checked
# here so the caller gets a clear error instead of a validation blob.
SF_MIN_CONTENT_BYTES = 1024


def _normalize_rating(val) -> int:
    """Convert a rating value (int, str, or label) to SF's numeric code."""
    if isinstance(val, int):
        return val
    if isinstance(val, str):
        return _RATING_REVERSE.get(val.lower().strip(), 0)
    return 0


class SoFurryError(RuntimeError):
    """An official-API call failed. Carries the parsed body when there is one."""

    def __init__(self, message: str, status: int = 0, body: dict | None = None):
        super().__init__(message)
        self.status = status
        self.body = body or {}


class SoFurryClient:
    """SoFurry client: PAT for writes, anonymous JSON for analytics."""

    def __init__(self, api_token: str = "", display_name: str = "",
                 proxy_url: str = "", proxy_key: str = ""):
        self.api_token = (api_token or "").strip()
        self.display_name = (display_name or "").lstrip("@").strip()

        # SoFurry blocks datacenter IPs across *every* host it owns — sofurry.com
        # AND api.sofurry.com both return a Cloudflare 403 from the GCP VM, while a
        # residential IP gets a normal response. So both surfaces need the proxy;
        # routing only the scrape half would work on the desktop and fail on the
        # server. (The transport forwards any target via x-target-url, so one
        # transport class serves both hosts.)
        def _transport():
            if proxy_url and proxy_key:
                from polling.cf_proxy import CloudflareProxyTransport
                return CloudflareProxyTransport(proxy_url, proxy_key)
            return httpx.AsyncHTTPTransport(retries=2)

        if proxy_url and proxy_key:
            logger.info("SoFurry client using CF proxy for both web + api: %s", proxy_url)

        self._web = httpx.AsyncClient(
            timeout=30.0,
            follow_redirects=True,
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.5",
                "Referer": "https://sofurry.com/",
            },
            transport=_transport(),
        )
        self._api = httpx.AsyncClient(
            base_url=SOFURRY_API_V1,
            timeout=60.0,
            follow_redirects=False,
            headers=self._api_headers(),
            transport=_transport(),
        )

    def _api_headers(self) -> dict:
        """Headers for the official API.

        ``Accept: application/json`` is not optional — without it an unauthenticated
        call 302s to the login page with an HTML body rather than returning a JSON
        error, and the client would read the redirect as something other than a
        credential failure.
        """
        h = {
            "Accept": "application/json",
            "User-Agent": "PawPoller (+https://github.com/knaughtykat01-prog/PawPoller)",
        }
        if self.api_token:
            h["Authorization"] = f"Bearer {self.api_token}"
        return h

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        await self.close()

    async def close(self) -> None:
        await self._web.aclose()
        await self._api.aclose()

    # -- Credentials ---------------------------------------------------

    def update_credentials(self, api_token: str, display_name: str = "") -> bool:
        """Re-point the client at a new token/handle. True when anything changed."""
        api_token = (api_token or "").strip()
        display_name = (display_name or "").lstrip("@").strip()
        changed = (api_token != self.api_token) or (display_name != self.display_name)
        if changed:
            self.api_token = api_token
            self.display_name = display_name
            self._api.headers.update(self._api_headers())
            if not api_token:
                self._api.headers.pop("Authorization", None)
        return changed

    @property
    def has_token(self) -> bool:
        return bool(self.api_token)

    # -- Official-API plumbing -----------------------------------------

    @staticmethod
    def _body(resp: httpx.Response) -> dict:
        try:
            data = resp.json()
        except Exception:
            return {}
        return data if isinstance(data, dict) else {"data": data}

    def _check(self, resp: httpx.Response, what: str) -> dict:
        """Raise on failure, else return the parsed body.

        SoFurry returns **HTTP 500 with a body saying ``statusCode: 400``** for an
        unsupported method, so the body's own code is the more truthful signal and
        is preferred in the message. Both are kept on the exception.
        """
        body = self._body(resp)
        inner = body.get("statusCode")
        # 3xx counts as failure: this client does not follow redirects, and the
        # one redirect the API emits is the login bounce you get without an
        # Accept: application/json header. Letting it through would hand the
        # caller an empty dict that looks like a successful-but-empty response.
        if resp.status_code >= 300 or (isinstance(inner, int) and inner >= 400):
            if resp.status_code in (301, 302, 303, 307, 308):
                raise SoFurryError(
                    f"SF {what} was redirected to {resp.headers.get('location', '?')} — "
                    "the request was not authenticated as JSON",
                    status=resp.status_code, body=body,
                )
            msg = (body.get("description") or body.get("message")
                   or resp.text[:200] or "unknown error")
            raise SoFurryError(
                f"SF {what} failed (HTTP {resp.status_code}"
                + (f", body says {inner}" if inner and inner != resp.status_code else "")
                + f"): {msg}",
                status=resp.status_code, body=body,
            )
        return body

    async def _require_token(self) -> None:
        if not self.api_token:
            raise SoFurryError(
                "SoFurry is not connected — add a Personal Access Token "
                f"(create one at {SOFURRY_PAT_URL})"
            )

    # -- Authentication / validation -----------------------------------

    async def validate_token(self) -> str | None:
        """Verify the PAT and return the account's canonical handle, else None.

        ``GET /v1/user/me`` is the cheap auth check; its ``handle`` is authoritative,
        so this also self-heals a mistyped or renamed ``sf_display_name`` instead of
        logging the "could not verify" warning the old session check produced.
        """
        if not self.api_token:
            logger.warning("SF: no API token configured")
            return None
        try:
            resp = await self._api.get("/v1/user/me")
            if resp.status_code == 401:
                logger.warning("SF: API token rejected (401) — it may have been revoked or expired")
                return None
            body = self._check(resp, "user/me")
            user = body.get("user") or body
            handle = user.get("handle") or user.get("username")
            if handle:
                self.display_name = handle
                return handle
            logger.warning("SF: /v1/user/me returned no handle")
            return None
        except SoFurryError as e:
            logger.warning("SF: token validation failed: %s", e)
            return None
        except Exception as e:
            logger.warning("SF: token validation error: %s", e)
            return None

    # Back-compat alias — callers that predate the PAT migration.
    async def validate_session(self) -> str | None:
        return await self.validate_token()

    # -- Gallery listing (official API) --------------------------------

    async def get_all_gallery_ids(self) -> list[dict]:
        """Every submission on the authenticated account, from the official API.

        This replaces a genuinely bad heuristic. The old implementation scraped
        ``/u/{handle}/gallery.data`` and, because that turbo-stream payload
        de-duplicates strings and lists folders alongside submissions, it took
        *every* 8-character alphanumeric token, subtracted the ids it knew weren't
        submissions, and handed the rest to the poller as unvalidated "candidates".
        Worse, an unauthenticated request to that endpoint is SFW-filtered, so an
        adult gallery returned **nothing at all**.

        ``GET /v1/user/{handle}/submissions`` returns the real list — paginated,
        with a true ``meta.total``, and including private works because the handle
        belongs to the authenticated user. No guessing, no SFW filtering.
        """
        await self._require_token()
        handle = self.display_name or await self.validate_token()
        if not handle:
            return []

        out: list[dict] = []
        page = 1
        healed = False
        while page <= 200:  # hard stop; 15/page → 3000 works
            try:
                resp = await self._api.get(
                    f"/v1/user/{handle}/submissions", params={"page": page})
                body = self._check(resp, f"gallery page {page}")
            except SoFurryError as e:
                # A 404 here means the PATH SEGMENT is not a real handle — this
                # endpoint is keyed on SoFurry's `handle`, which is NOT the
                # display name and need not resemble it. Measured 2026-08-23:
                # `sf_display_name` was "SecondFur" while /v1/user/me reported
                # handle "SecondHandle", so every listing 404'd and the account
                # discovered nothing, for ever.
                #
                # validate_token() reads the authoritative handle from
                # /v1/user/me and its docstring already promised to "self-heal a
                # mistyped or renamed sf_display_name" — but it was only ever
                # reached via `self.display_name or …`, so it could run only when
                # there was nothing to heal. A self-heal that cannot fire is not
                # a self-heal. This is where it actually fires.
                if getattr(e, "status", 0) == 404 and not healed:
                    healed = True
                    real = await self.validate_token()
                    if real and real != handle:
                        logger.warning(
                            "SF: '%s' is not a SoFurry handle — the account's real "
                            "handle is '%s'. Retrying the gallery listing with it.",
                            handle, real)
                        handle = real
                        continue
                logger.warning("SF: gallery listing failed on page %d: %s", page, e)
                break

            rows = body.get("data") or []
            for row in rows:
                sid = row.get("id")
                if not sid:
                    continue
                out.append({
                    "submission_id": str(sid),
                    "title": row.get("title") or "",
                    "thumbnail_url": row.get("thumbUrl") or "",
                    # privacy travels with the row because the official listing
                    # includes the owner's PRIVATE works, which the anonymous
                    # stats endpoint cannot read. The poller needs to tell those
                    # apart from junk ids rather than reporting them as junk.
                    "privacy": _safe_int(row.get("privacy")),
                })

            meta = body.get("meta") or {}
            if not rows or page >= (meta.get("last_page") or page):
                break
            page += 1
            await asyncio.sleep(config.SF_REQUEST_DELAY_SECONDS)

        logger.info("SF: %d submissions listed via the official API", len(out))
        return out

    # -- Submission detail / analytics (anonymous JSON) -----------------

    async def get_submission_detail(self, submission_id: str) -> dict:
        """Stats + metadata for one submission. No authentication required.

        **The official API returns no statistics at all**, so this stays on
        sofurry.com. It reads ``GET /api/submission/{id}``, which serves clean JSON
        anonymously for published works (Adult included — verified against a live
        ``rating: 20`` work).

        3.4.0 changed *which* endpoint this uses, and that fixed a real data bug.
        The previous implementation regex-scraped the turbo-stream payload at
        ``/s/{id}.data`` with a pattern that assumed a value always sits immediately
        after its key. That payload is devalue-encoded with a **de-duplicated value
        table**: large unique numbers like a view count are emitted fresh and matched
        fine, but small integers that already appear in the table are not re-emitted,
        so the key was followed by the *next key* and the parse silently returned 0.
        Like counts are exactly the small integers that hit this — favourites were
        systematically under-reported. The JSON endpoint has no such ambiguity.

        Comment count still comes from the turbo-stream payload, because no JSON
        endpoint exposes it (``/api/comments*`` variants all 404). That parse uses a
        pattern anchored between two literals (``"total",N,"hasMore"``) rather than a
        bare key lookup, so it does not share the failure mode above. It is
        best-effort: on failure ``comments_count`` is **None**, meaning "unknown", and
        the poller preserves the previous value rather than writing a bogus 0 that
        would read as "all comments deleted" and then re-fire as new comments.

        On total failure the stat fields stay 0; the poller's zero-view guard then
        skips the work for the cycle rather than persisting a bogus baseline.
        """
        detail: dict[str, Any] = {
            "submission_id": submission_id,
            "title": "",
            "username": self.display_name,
            "posted_at": "",
            "content_type": "",
            "rating": "",
            "thumbnail_url": "",
            "description": "",
            "keywords": [],
            "link": f"{SOFURRY_BASE}/s/{submission_id}",
            "views": 0,
            "favorites_count": 0,
            "comments_count": None,
        }

        try:
            resp = await self._web.get(
                f"{SOFURRY_API}/submission/{submission_id}",
                headers={"Accept": "application/json"},
            )
            if resp.status_code != 200:
                logger.warning("SF: /api/submission/%s returned HTTP %d",
                               submission_id, resp.status_code)
                return detail
            sub = (resp.json() or {}).get("submission") or {}
            detail["title"] = sub.get("title") or ""
            detail["description"] = sub.get("description") or ""
            detail["posted_at"] = sub.get("publishedAt") or ""
            detail["content_type"] = str(sub.get("category") or "")
            # The old turbo-stream path never populated rating at all (it stayed "").
            # The JSON endpoint gives the int code, so store the human label the
            # dashboard can show directly.
            detail["rating"] = _RATING_MAP.get(_safe_int(sub.get("rating")), "")
            detail["thumbnail_url"] = sub.get("thumbUrl") or ""
            detail["keywords"] = [t for t in (sub.get("tags") or []) if isinstance(t, str)]
            detail["views"] = _safe_int(sub.get("views"))
            detail["favorites_count"] = _safe_int(sub.get("likes"))
        except Exception as e:
            logger.warning("Failed to fetch SF submission %s: %s", submission_id, e)
            return detail

        detail["comments_count"] = await self._fetch_comment_count(submission_id)
        return detail

    async def _fetch_comment_count(self, submission_id: str) -> int | None:
        """Comment total from the turbo-stream payload. None means "unknown"."""
        try:
            resp = await self._web.get(
                f"{SOFURRY_BASE}/s/{submission_id}.data", headers={"Accept": "*/*"})
            if resp.status_code != 200:
                logger.debug("SF: /s/%s.data returned HTTP %d for comment count",
                             submission_id, resp.status_code)
                return None
            m = re.search(r'"total",(\d+),"hasMore"', resp.text)
            return int(m.group(1)) if m else None
        except Exception as e:
            logger.debug("SF: comment count fetch failed for %s: %s", submission_id, e)
            return None

    async def get_page(self, url: str) -> httpx.Response:
        """Fetch a sofurry.com page on the unauthenticated web client.

        Used by the importer to scrape rendered story HTML, which no API returns.
        Public so callers don't reach into the transport directly.
        """
        return await self._web.get(url)

    async def get_submission_details_batch(self, submission_ids: list[str]) -> list[dict]:
        """Fetch details for multiple submissions sequentially with rate limiting."""
        details: list[dict] = []
        for i, sid in enumerate(submission_ids):
            try:
                details.append(await self.get_submission_detail(sid))
            except Exception as e:
                logger.warning("Failed to fetch SF submission %s: %s", sid, e)
            if i < len(submission_ids) - 1:
                await asyncio.sleep(config.SF_REQUEST_DELAY_SECONDS)
        return details

    # -- Followers (anonymous JSON) ------------------------------------

    async def get_follower_count(self) -> int:
        """Follower count from the profile API (login-free).

        Kept on sofurry.com deliberately: the official ``GET /v1/user/{handle}``
        carries no ``followerCount`` — verified against the OpenAPI schema and a
        live response.
        """
        try:
            resp = await self._web.get(
                f"{SOFURRY_API}/profile",
                params={"handle": self.display_name},
                headers={"Accept": "application/json"},
            )
            if resp.status_code == 200:
                user = (resp.json() or {}).get("user", {})
                return _safe_int(user.get("followerCount"))
            logger.warning("SF: /api/profile returned HTTP %d for follower count",
                           resp.status_code)
        except Exception as e:
            logger.warning("Failed to get SF follower count: %s", e)
        return 0

    async def scrape_followers(self) -> list[str]:
        """Follower handles via the profile API (login-free).

        ``GET /api/followers?handle={handle}&mode=followers&page={0-based}``, 20 per
        page. Returns [] on failure — the poller's prune is guarded on a non-empty
        result, so a failed fetch never wipes the watcher list.
        """
        followers: list[str] = []
        seen: set[str] = set()
        page = 0

        for _page_safety in range(500):  # hard cap: 500 pages * 20 = 10k followers
            try:
                resp = await self._web.get(
                    f"{SOFURRY_API}/followers",
                    params={"handle": self.display_name, "mode": "followers",
                            "page": str(page)},
                    headers={"Accept": "application/json"},
                )
                if resp.status_code != 200:
                    logger.warning("SF: /api/followers page %d returned HTTP %d",
                                   page, resp.status_code)
                    break
                data = resp.json() or {}
            except Exception as e:
                logger.warning("SF: follower fetch failed on page %d: %s", page, e)
                break

            users = data.get("users") or []
            for u in users:
                handle = u.get("handle") or u.get("username")
                if handle and handle not in seen:
                    seen.add(handle)
                    followers.append(handle)

            if not data.get("hasNextPage") or not users:
                break
            page += 1
            await asyncio.sleep(config.SF_REQUEST_DELAY_SECONDS)

        logger.info("SF: scraped %d followers via /api/followers", len(followers))
        return followers

    # -- Reads against the official API --------------------------------

    async def get_submission(self, submission_id: str) -> dict:
        """Full submission object from the official API (includes private works)."""
        await self._require_token()
        resp = await self._api.get(f"/v1/submission/{submission_id}")
        if resp.status_code == 404:
            return {}
        body = self._check(resp, f"get submission {submission_id}")
        return body.get("submission") or body

    async def get_content_ids(self, submission_id: str) -> list[str]:
        """The submission's content item ids, in their stored order."""
        try:
            sub = await self.get_submission(submission_id)
        except SoFurryError as e:
            logger.warning("SF: could not list content for %s: %s", submission_id, e)
            return []
        out = []
        for item in (sub.get("content") or []):
            cid = item.get("contentId") or item.get("id")
            if cid:
                out.append(str(cid))
        return out

    # -- Writes (official API) -----------------------------------------

    async def upload_content(self, submission_id: str, file_path: str,
                             content_type: str | None = None) -> str | None:
        """Append a content item (chapter / image) to a submission.

        SoFurry rejects anything under 1 KB with a 422, so that is checked up front
        to produce a comprehensible error. content_type is inferred from the
        extension when not given, so the same call carries stories and artwork.
        """
        await self._require_token()
        with open(file_path, "rb") as f:
            file_data = f.read()
        if len(file_data) < SF_MIN_CONTENT_BYTES:
            raise SoFurryError(
                f"SF rejects content under {SF_MIN_CONTENT_BYTES} bytes; "
                f"{os.path.basename(file_path)} is {len(file_data)} bytes"
            )
        filename = os.path.basename(file_path)
        if content_type is None:
            ext = os.path.splitext(filename)[1].lstrip(".").lower()
            content_type = {
                "png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg",
                "gif": "image/gif", "webp": "image/webp",
            }.get(ext, "text/html")

        resp = await self._api.post(
            f"/v1/submission/{submission_id}/content",
            files={"file": (filename, file_data, content_type)},
            timeout=120.0,
        )
        body = self._check(resp, f"upload content to {submission_id}")
        return body.get("contentId")

    async def update_content(self, submission_id: str, content_id: str, *,
                             body_html: str | None = None,
                             title: str | None = None,
                             description: str | None = None) -> dict:
        """Replace one content item's body and/or title **in place**.

        This exists because **the official API cannot delete content**. Both DELETE
        routes are unsupported and Laravel ``_method`` spoofing is rejected too (the
        spoof is honoured, then routing refuses — so it is a genuine absence, not a
        transport quirk). The old delete-then-reupload dance is therefore replaced by
        updating the existing item, which is what the callers actually wanted and
        avoids the window where a submission briefly held both copies.

        The consequence to know: if a story's chapter count *shrinks*, the surplus
        items cannot be removed through the API at all and must be deleted in the
        SoFurry UI. Callers should log loudly when that happens.
        """
        await self._require_token()
        payload: dict[str, Any] = {}
        if title is not None:
            payload["title"] = title
        if description is not None:
            payload["description"] = description
        if body_html is not None:
            payload["binary"] = body_html
        resp = await self._api.post(
            f"/v1/submission/{submission_id}/content/{content_id}",
            json=payload, timeout=120.0,
        )
        return self._check(resp, f"update content {content_id}")

    async def set_content_title(self, submission_id: str, content_id: str,
                                title: str) -> None:
        """Set the chapter title on one content item."""
        await self.update_content(submission_id, content_id, title=title or "")

    async def _set_metadata(
        self, submission_id: str, *,
        title: str, description: str, tags: list[str] | None,
        category: int, sub_type: int, rating: int, privacy: int,
        allow_comments: bool = True, allow_downloads: bool = True,
        is_wip: bool = False, optimize: bool = True,
        pixel_perfect: bool = False, is_advert: bool = False,
        content_order: list[str] | None = None,
    ) -> dict:
        """POST the full metadata block. Setting privacy=3 publishes.

        The official API accepts a JSON body, where the internal one demanded
        multipart with repeated ``artistTags[]`` fields — tags are a plain array now.
        Underscores become spaces to match SoFurry's tag convention.
        """
        await self._require_token()
        payload: dict[str, Any] = {
            "title": title or "",
            "description": description or "",
            "category": int(category),
            "type": int(sub_type),
            "rating": int(rating),
            "privacy": int(privacy),
            "allowComments": bool(allow_comments),
            "allowDownloads": bool(allow_downloads),
            "isWip": bool(is_wip),
            "optimize": bool(optimize),
            "pixelPerfect": bool(pixel_perfect),
            "isAdvert": bool(is_advert),
            "artistTags": [t.replace("_", " ") for t in (tags or [])],
        }
        if content_order:
            payload["contentOrder"] = [str(c) for c in content_order]
        resp = await self._api.post(f"/v1/submission/{submission_id}", json=payload)
        return self._check(resp, f"set metadata on {submission_id}")

    async def set_content_order(self, submission_id: str, content_ids: list[str]) -> None:
        """Reorder a submission's chapters without touching other metadata.

        Reads current state first so the required metadata fields round-trip
        unchanged — the endpoint takes a whole metadata block, not a patch.
        """
        sub = await self.get_submission(submission_id)
        if not sub:
            return
        await self._set_metadata(
            submission_id,
            title=sub.get("title") or "",
            description=sub.get("description") or "",
            tags=sub.get("artistTags") or [],
            category=_as_category_int(sub.get("category")),
            sub_type=_as_type_int(sub.get("type")),
            rating=_safe_int(sub.get("rating")),
            privacy=_safe_int(sub.get("privacy")) or 1,
            allow_comments=bool(sub.get("allowComments", True)),
            allow_downloads=bool(sub.get("allowDownloads", True)),
            is_wip=bool(sub.get("isWorkInProgress", False)),
            content_order=content_ids,
        )

    async def create_submission(
        self,
        file_path: str,
        *,
        title: str = "",
        description: str = "",
        tags: list[str] | None = None,
        category: int = 20,
        sub_type: int = 21,
        rating: int = 20,
        privacy: int = 3,
        thumbnail_path: str | None = None,
    ) -> dict:
        """Create and publish a submission via the official API.

        Three steps, matching the documented workflow:
          1. ``PUT /v1/submission``                     → mint an empty private draft
          2. ``POST /v1/submission/{id}/content``       → upload the file (>= 1 KB)
          3. ``POST /v1/submission/{id}``               → metadata; privacy=3 publishes

        ``thumbnail_path`` is accepted for signature compatibility but **cannot be
        honoured** — the official API exposes no thumbnail or cover upload route
        (all four plausible paths 404, and ``thumbUrl``/``coverUrl`` are read-only),
        so a custom thumbnail is logged and skipped rather than silently dropped.
        Text works auto-generate one anyway.

        Returns a dict with 'submission_id' and 'url'.
        """
        await self._require_token()

        resp = await self._api.put("/v1/submission")
        body = self._check(resp, "create submission")
        submission_id = body.get("id")
        if not submission_id:
            raise SoFurryError(f"SF: create returned no id: {resp.text[:200]}")
        logger.info("SF: created submission %s", submission_id)

        await self.upload_content(submission_id, file_path)
        logger.info("SF: uploaded content to submission %s", submission_id)

        await self._set_metadata(
            submission_id,
            title=title, description=description, tags=tags,
            category=category, sub_type=sub_type, rating=rating, privacy=privacy,
        )

        if thumbnail_path and os.path.isfile(thumbnail_path):
            logger.warning(
                "SF: custom thumbnail ignored for %s — the official API has no "
                "thumbnail upload endpoint; set it in the SoFurry UI if needed.",
                submission_id,
            )

        url = f"{SOFURRY_BASE}/s/{submission_id}"
        logger.info("SF: published submission %s — %s", submission_id, url)
        return {"submission_id": str(submission_id), "url": url}

    async def edit_submission(
        self,
        submission_id: str,
        *,
        title: str | None = None,
        description: str | None = None,
        tags: list[str] | None = None,
        rating: int | None = None,
        privacy: int | None = None,
    ) -> dict:
        """Edit metadata on an existing submission.

        Reads current state, overlays only the caller's changes, and posts the whole
        block back — the endpoint replaces metadata rather than patching it, so every
        unspecified field must mirror the server or an edit would clobber it.

        Privacy defaults to the server's current value, preserving the long-standing
        invariant that an edit never silently downgrades a public work to Private.
        """
        await self._require_token()
        current = await self.get_submission(submission_id)
        if not current:
            raise SoFurryError(f"SF: submission {submission_id} not found")

        result = await self._set_metadata(
            submission_id,
            title=title if title is not None else (current.get("title") or ""),
            description=(description if description is not None
                         else (current.get("description") or "")),
            tags=tags if tags is not None else (current.get("artistTags") or []),
            category=_as_category_int(current.get("category")),
            sub_type=_as_type_int(current.get("type")),
            rating=(_normalize_rating(rating) if rating is not None
                    else _safe_int(current.get("rating"))),
            privacy=(int(privacy) if privacy is not None
                     else (_safe_int(current.get("privacy")) or 1)),
            allow_comments=bool(current.get("allowComments", True)),
            allow_downloads=bool(current.get("allowDownloads", True)),
            is_wip=bool(current.get("isWorkInProgress", False)),
        )
        return {
            "submission_id": str(submission_id),
            "url": f"{SOFURRY_BASE}/s/{submission_id}",
            "raw": result,
        }


def _safe_int(val: Any) -> int:
    try:
        return int(val)
    except (TypeError, ValueError):
        return 0


def _as_category_int(val: Any) -> int:
    """Category as an int, accepting either the int the official API returns or the
    display string the internal API used to echo."""
    if isinstance(val, int):
        return val
    if isinstance(val, str):
        if val.isdigit():
            return int(val)
        return _SF_CATEGORY_STR_TO_INT.get(val.lower().strip(), 20)
    return 20


def _as_type_int(val: Any) -> int:
    """Type as an int, accepting either an int or the legacy display string."""
    if isinstance(val, int):
        return val
    if isinstance(val, str):
        if val.isdigit():
            return int(val)
        return _SF_TYPE_STR_TO_INT.get(val.lower().replace(" ", "").strip(), 21)
    return 21
