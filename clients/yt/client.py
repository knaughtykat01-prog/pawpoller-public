"""YouTube Data API v3 client — MEDIAPLATS §6 (4.24.0).

Built against developers.google.com/youtube/v3 as fetched on 2026-09-08 (videos.insert,
videos.list, thumbnails.set, channels.list) plus Google's OAuth 2.0 for installed / web apps:

- **OAuth.** Authorise at ``accounts.google.com/o/oauth2/v2/auth`` with ``response_type=code``,
  ``scope``, ``access_type=offline`` + ``prompt=consent`` (the only way a refresh token is issued
  again once one exists), ``state`` and PKCE (S256). Exchange / refresh at
  ``oauth2.googleapis.com/token`` as form data; Google's refresh token is NOT rotated (the same one
  keeps working) — but an app left in *Testing* on the consent screen has its refresh tokens expire
  after 7 days, the Gmail trap. Requests carry ``Authorization: Bearer``.
- **Scopes.** ``youtube.upload`` (upload) + ``youtube.readonly`` (the channel and its videos' stats);
  ``youtube.force-ssl`` for ``videos.update`` and thumbnails. This client asks for
  ``youtube.upload youtube.force-ssl youtube.readonly``.
- **Upload** is resumable: ``POST upload/youtube/v3/videos?uploadType=resumable&part=snippet,status``
  with the JSON metadata and ``X-Upload-Content-Type`` / ``X-Upload-Content-Length`` → a
  ``Location`` session URL; ``PUT`` chunks with ``Content-Range: bytes a-b/total`` (multiples of
  256 KiB; 8 MiB here); ``308`` answers carry ``Range: bytes=0-N`` for the bytes received; the last
  chunk answers ``200`` / ``201`` with the video resource. Max 256 GB, ``video/*``. Quota: **one call
  of the 100-per-day upload bucket**; everything else 10,000 units a day (``videos.list`` 1,
  ``thumbnails.set`` ~50, ``videos.update`` ~50).
- **Unverified projects created after 28 July 2020 upload as private** — whatever ``privacyStatus``
  says — until YouTube's API compliance audit passes. The poster asks for what the piece wants and
  reports what came back.
- ``thumbnails.set`` (``upload/youtube/v3/thumbnails/set?videoId=``, JPEG / PNG ≤ 2 MB) — a custom
  thumbnail needs a phone-verified channel; a 403 there is reported, not fatal.
- Discovery: ``channels.list?mine=true&part=contentDetails,statistics,status`` → the uploads
  playlist (``relatedPlaylists.uploads``), ``subscriberCount``, ``longUploadsStatus``; then
  ``playlistItems.list?playlistId=…&part=contentDetails`` (50 a page) → video ids;
  ``videos.list?id=…&part=snippet,statistics,status`` (≤ 50 ids a call) → the numbers.

❓ Not proven live: the operator's Google project must exist and the consent screen be configured;
every shape is the documented one until the first real call.
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import secrets
import time
import urllib.parse
from typing import Any

import httpx

logger = logging.getLogger(__name__)

API_BASE = "https://www.googleapis.com/youtube/v3"
UPLOAD_BASE = "https://www.googleapis.com/upload/youtube/v3"
AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
SCOPES = ("https://www.googleapis.com/auth/youtube.upload",
          "https://www.googleapis.com/auth/youtube.force-ssl",
          "https://www.googleapis.com/auth/youtube.readonly")
HTTP_TIMEOUT = 30.0
CHUNK = 8 * 1024 * 1024                  # a multiple of 256 KiB, as the protocol wants
CHUNK_TIMEOUT = 600.0
PAGE = 50
MAX_PAGES = 40                           # 2 000 videos — a stop, not a target
MAX_BYTES = 256 * 1024 * 1024 * 1024
THUMB_MAX_BYTES = 2 * 1024 * 1024
TITLE_MAX = 100
DEFAULT_CATEGORY = "1"                   # Film & Animation
PRIVACY = ("private", "unlisted", "public")

ACCEPTED_VIDEO = ("mp4", "webm", "mov", "m4v")
CATEGORIES = {
    "1": "Film & Animation", "2": "Autos & Vehicles", "10": "Music", "15": "Pets & Animals", "17": "Sports",
    "19": "Travel & Events", "20": "Gaming", "22": "People & Blogs", "23": "Comedy", "24": "Entertainment",
    "25": "News & Politics", "26": "Howto & Style", "27": "Education", "28": "Science & Technology",
}


class YtAuthError(Exception):
    """The token was refused, or there is none — the operator must re-authorise."""


class YtQuotaError(Exception):
    """403 quotaExceeded / uploadLimitExceeded — try again after the Pacific-time daily reset."""


def _safe_int(v: Any) -> int:
    try:
        return int(v or 0)
    except (TypeError, ValueError):
        return 0


def pkce_pair() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(64)
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return verifier, base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def authorize_url(client_id: str, redirect_uri: str, challenge: str, state: str) -> str:
    params = {
        "client_id": client_id, "redirect_uri": redirect_uri, "response_type": "code",
        "scope": " ".join(SCOPES), "access_type": "offline", "prompt": "consent",
        "include_granted_scopes": "true", "state": state,
        "code_challenge": challenge, "code_challenge_method": "S256",
    }
    return f"{AUTH_URL}?{urllib.parse.urlencode(params)}"


def _api_error(r: httpx.Response) -> str:
    try:
        err = r.json().get("error", {})
        reasons = ",".join(e.get("reason", "") for e in err.get("errors", []) if isinstance(e, dict))
        return f"{err.get('message') or r.text[:200]}" + (f" [{reasons}]" if reasons else "")
    except Exception:
        return r.text[:200] or f"HTTP {r.status_code}"


def _quota_hit(r: httpx.Response) -> bool:
    if r.status_code != 403:
        return False
    try:
        reasons = {e.get("reason") for e in r.json().get("error", {}).get("errors", [])}
    except Exception:
        return False
    return bool(reasons & {"quotaExceeded", "uploadLimitExceeded", "dailyLimitExceeded", "rateLimitExceeded"})


class YtClient:
    def __init__(self, client_id: str = "", client_secret: str = "", access_token: str = "",
                 refresh_token: str = "", expires_at: float = 0.0):
        self.client_id, self.client_secret = client_id, client_secret
        self.access_token, self.refresh_token = access_token, refresh_token
        self.expires_at = float(expires_at or 0.0)
        self.tokens_changed = False
        self._client: httpx.AsyncClient | None = None
        self._channel: dict | None = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        await self.close()

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=HTTP_TIMEOUT)
        return self._client

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self.access_token}", "Accept": "application/json"}

    # ── OAuth ────────────────────────────────────────────────────────────────

    async def _token_request(self, data: dict) -> dict:
        r = await self._http().post(TOKEN_URL, data={**data, "client_id": self.client_id,
                                                     "client_secret": self.client_secret})
        try:
            body = r.json()
        except Exception:
            body = {}
        if r.status_code >= 400 or body.get("error"):
            msg = body.get("error_description") or body.get("error") or f"HTTP {r.status_code}"
            if body.get("error") == "invalid_grant":
                msg += (" — the refresh token is dead. A Google app still in Testing expires them every 7 days; "
                        "re-authorise in Settings (or publish the app's consent screen).")
            raise YtAuthError(f"Google auth failed: {msg}")
        self.access_token = body.get("access_token", "") or self.access_token
        if body.get("refresh_token"):
            self.refresh_token = body["refresh_token"]
        self.expires_at = time.time() + max(60, _safe_int(body.get("expires_in")) - 60)
        self.tokens_changed = True
        return body

    async def exchange_code(self, code: str, redirect_uri: str, verifier: str) -> dict:
        return await self._token_request({"grant_type": "authorization_code", "code": code,
                                          "redirect_uri": redirect_uri, "code_verifier": verifier})

    async def refresh(self) -> dict:
        if not self.refresh_token:
            raise YtAuthError("YouTube is not authorised — connect it in Settings")
        return await self._token_request({"grant_type": "refresh_token", "refresh_token": self.refresh_token})

    async def _ensure_token(self) -> None:
        if not self.access_token or time.time() >= self.expires_at:
            await self.refresh()

    async def _request(self, method: str, url: str, *, retry_auth: bool = True, **kw) -> httpx.Response:
        await self._ensure_token()
        headers = {**self._headers(), **kw.pop("headers", {})}
        r = await self._http().request(method, url, headers=headers, **kw)
        if r.status_code == 401 and retry_auth:
            await self.refresh()
            return await self._request(method, url, retry_auth=False, **kw)
        if r.status_code == 401:
            raise YtAuthError("Google rejected the token (401) — re-authorise in Settings")
        if _quota_hit(r):
            raise YtQuotaError(f"YouTube quota: {_api_error(r)}")
        return r

    async def _get_json(self, path: str, params: dict | None = None) -> dict | None:
        r = await self._request("GET", f"{API_BASE}/{path}", params=params)
        if r.status_code >= 400:
            logger.warning("YouTube %s: %s", path, _api_error(r))
            return None
        try:
            return r.json()
        except Exception:
            return None

    # ── the channel ──────────────────────────────────────────────────────────

    async def get_channel(self) -> dict | None:
        """The authenticated user's channel: id, title, uploads playlist, subscribers, upload status."""
        if self._channel is None:
            data = await self._get_json("channels", {"part": "snippet,contentDetails,statistics,status", "mine": "true"})
            items = (data or {}).get("items") or []
            if not items:
                return None
            c = items[0]
            st = c.get("statistics") or {}
            self._channel = {
                "id": c.get("id", ""),
                "title": (c.get("snippet") or {}).get("title", ""),
                "handle": (c.get("snippet") or {}).get("customUrl", "") or "",
                "uploads": ((c.get("contentDetails") or {}).get("relatedPlaylists") or {}).get("uploads", ""),
                "subscribers": None if st.get("hiddenSubscriberCount") else _safe_int(st.get("subscriberCount")),
                "videos": _safe_int(st.get("videoCount")),
                "long_uploads": (c.get("status") or {}).get("longUploadsStatus", ""),
            }
        return self._channel

    async def validate_session(self) -> str | None:
        if not (self.refresh_token or self.access_token):
            return None
        ch = await self.get_channel()
        return (ch or {}).get("handle") or (ch or {}).get("title") or None

    async def get_follower_count(self) -> int | None:
        ch = await self.get_channel()
        return (ch or {}).get("subscribers")

    # ── videos ───────────────────────────────────────────────────────────────

    async def get_my_video_ids(self) -> list[str]:
        ch = await self.get_channel()
        playlist = (ch or {}).get("uploads")
        if not playlist:
            return []
        out: list[str] = []
        token = None
        for _ in range(MAX_PAGES):
            params = {"part": "contentDetails", "playlistId": playlist, "maxResults": PAGE}
            if token:
                params["pageToken"] = token
            data = await self._get_json("playlistItems", params)
            if not data:
                break
            for it in data.get("items") or []:
                vid = (it.get("contentDetails") or {}).get("videoId")
                if vid and vid not in out:
                    out.append(vid)
            token = data.get("nextPageToken")
            if not token:
                break
        return out

    async def get_videos(self, ids: list[str]) -> list[dict]:
        out: list[dict] = []
        for i in range(0, len(ids), PAGE):
            batch = ids[i:i + PAGE]
            data = await self._get_json("videos", {"part": "snippet,statistics,status,contentDetails", "id": ",".join(batch)})
            for v in (data or {}).get("items") or []:
                out.append(self.parse_video(v))
        return out

    @staticmethod
    def parse_video(v: dict) -> dict:
        sn, st, stt = v.get("snippet") or {}, v.get("statistics") or {}, v.get("status") or {}
        thumbs = sn.get("thumbnails") or {}
        thumb = (thumbs.get("high") or thumbs.get("medium") or thumbs.get("default") or {}).get("url", "")
        return {
            "submission_id": v.get("id", ""),
            "title": sn.get("title", "") or "",
            "description": sn.get("description", "") or "",
            "tags": list(sn.get("tags") or []),
            "category_id": str(sn.get("categoryId", "") or ""),
            "privacy": stt.get("privacyStatus", "") or "",
            "upload_status": stt.get("uploadStatus", "") or "",
            "username": sn.get("channelTitle", "") or "",
            "link": f"https://www.youtube.com/watch?v={v.get('id', '')}" if v.get("id") else "",
            "thumbnail_url": thumb,
            "duration": (v.get("contentDetails") or {}).get("duration", "") or "",
            "posted_at": sn.get("publishedAt", "") or "",
            "views": _safe_int(st.get("viewCount")),
            "favorites_count": _safe_int(st.get("likeCount")),
            "comments_count": _safe_int(st.get("commentCount")),
        }

    async def upload_video(self, *, file_path: str, title: str, description: str = "", tags: list[str] | None = None,
                           category_id: str = DEFAULT_CATEGORY, privacy: str = "private",
                           made_for_kids: bool = False, mime: str = "video/mp4") -> dict:
        """The resumable upload. Returns {"success", "id", "url", "privacy"} or {"success": False, "error"}."""
        if not os.path.isfile(file_path):
            return {"success": False, "error": f"file not found: {file_path}"}
        size = os.path.getsize(file_path)
        if size > MAX_BYTES:
            return {"success": False, "error": "YouTube takes files up to 256 GB"}
        meta = {"snippet": {"title": (title or os.path.basename(file_path))[:TITLE_MAX], "description": description or "",
                            "tags": [t for t in (tags or []) if t][:500], "categoryId": category_id or DEFAULT_CATEGORY},
                "status": {"privacyStatus": privacy if privacy in PRIVACY else "private",
                           "selfDeclaredMadeForKids": bool(made_for_kids)}}
        try:
            r = await self._request("POST", f"{UPLOAD_BASE}/videos", params={"uploadType": "resumable", "part": "snippet,status"},
                                    content=json.dumps(meta).encode("utf-8"),
                                    headers={"Content-Type": "application/json; charset=UTF-8",
                                             "X-Upload-Content-Type": mime, "X-Upload-Content-Length": str(size)})
        except YtQuotaError as e:
            return {"success": False, "error": str(e)}
        if r.status_code >= 400 or not r.headers.get("location"):
            return {"success": False, "error": f"YouTube would not open an upload session: {_api_error(r)}"}
        session = r.headers["location"]
        body: dict = {}
        async with httpx.AsyncClient(timeout=CHUNK_TIMEOUT) as up:
            with open(file_path, "rb") as fh:
                offset = 0
                while offset < size:
                    blob = fh.read(CHUNK)
                    end = offset + len(blob) - 1
                    pr = await up.put(session, content=blob,
                                      headers={"Authorization": f"Bearer {self.access_token}", "Content-Type": mime,
                                               "Content-Range": f"bytes {offset}-{end}/{size}"})
                    if pr.status_code == 308:
                        rng = pr.headers.get("range", "")
                        offset = int(rng.rsplit("-", 1)[-1]) + 1 if rng else end + 1
                        fh.seek(offset)
                        continue
                    if pr.status_code in (200, 201):
                        try:
                            body = pr.json()
                        except Exception:
                            body = {}
                        offset = size
                        break
                    return {"success": False, "error": f"YouTube upload failed at byte {offset} (HTTP {pr.status_code}): "
                                                       f"{_api_error(pr)}"}
        vid = body.get("id", "")
        if not vid:
            return {"success": False, "error": "upload finished but YouTube returned no video id"}
        parsed = self.parse_video(body)
        return {"success": True, "id": vid, "url": parsed["link"], "privacy": parsed["privacy"] or meta["status"]["privacyStatus"]}

    async def set_thumbnail(self, video_id: str, image_path: str) -> dict:
        if not os.path.isfile(image_path) or os.path.getsize(image_path) > THUMB_MAX_BYTES:
            return {"success": False, "error": "thumbnail missing or over 2 MB"}
        with open(image_path, "rb") as fh:
            data = fh.read()
        try:
            r = await self._request("POST", f"{UPLOAD_BASE}/thumbnails/set", params={"videoId": video_id, "uploadType": "media"},
                                    content=data, headers={"Content-Type": "image/jpeg"})
        except YtQuotaError as e:
            return {"success": False, "error": str(e)}
        if r.status_code >= 400:
            return {"success": False, "error": f"thumbnail refused: {_api_error(r)}"}
        return {"success": True}

    async def update_video(self, video_id: str, *, title: str | None = None, description: str | None = None,
                           tags: list[str] | None = None, category_id: str | None = None,
                           privacy: str | None = None) -> dict:
        """``videos.update`` needs the whole snippet, so the current one is read first."""
        current = await self.get_videos([video_id])
        if not current:
            return {"success": False, "error": "YouTube does not know that video (deleted, or not this channel's)"}
        cur = current[0]
        snippet = {"title": (title if title is not None else cur["title"])[:TITLE_MAX],
                   "description": description if description is not None else cur["description"],
                   "tags": tags if tags is not None else cur["tags"],
                   "categoryId": category_id or cur["category_id"] or DEFAULT_CATEGORY}
        body: dict = {"id": video_id, "snippet": snippet}
        parts = "snippet"
        if privacy in PRIVACY:
            body["status"] = {"privacyStatus": privacy}
            parts = "snippet,status"
        try:
            r = await self._request("PUT", f"{API_BASE}/videos", params={"part": parts},
                                    content=json.dumps(body).encode("utf-8"),
                                    headers={"Content-Type": "application/json; charset=UTF-8"})
        except YtQuotaError as e:
            return {"success": False, "error": str(e)}
        if r.status_code >= 400:
            return {"success": False, "error": f"YouTube update failed: {_api_error(r)}"}
        return {"success": True, "id": video_id, "url": f"https://www.youtube.com/watch?v={video_id}"}
