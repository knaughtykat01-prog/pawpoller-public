"""SoundCloud API client — MEDIAPLATS §3 (4.22.0).

Built against the developer guide and reference as fetched on 2026-09-08
(developers.soundcloud.com/docs/api/guide, /reference, /rate-limits):

- **OAuth 2.1, PKCE required.** Authorise at ``secure.soundcloud.com/authorize`` with
  ``response_type=code``, ``code_challenge`` (S256) and ``state``; exchange at
  ``secure.soundcloud.com/oauth/token`` as form data with ``grant_type=authorization_code``,
  ``client_id``, ``client_secret``, ``redirect_uri``, ``code_verifier``, ``code``.
- **The access token lasts about an hour; the refresh token is single-use** — every
  refresh returns a new one, so whoever calls :meth:`refresh` must persist what comes
  back (the FurryNetwork lesson: a rotated token that is not written down is a dead
  account at the next renewal).
- Requests carry ``Authorization: OAuth <token>`` (not Bearer) and
  ``Accept: application/json; charset=utf-8``. Paginated lists take
  ``linked_partitioning=true`` and answer with ``collection`` + ``next_href``; up to 200
  per page.
- ``POST /tracks`` is multipart: ``track[title]``, ``track[asset_data]`` (the audio),
  ``track[artwork_data]``, ``track[description]``, ``track[sharing]`` public/private,
  ``track[genre]``, ``track[tag_list]``; up to 4 GB and 24 hours; AIFF, WAVE, FLAC, OGG,
  MP2, MP3, AAC, AMR, WMA. ``PUT /tracks/{id}`` updates the metadata (the audio cannot be
  replaced). The track's ``permalink_url`` is its page; ``playback_count``,
  ``likes_count`` (older responses ``favoritings_count``), ``comment_count`` and
  ``reposts_count`` are the counts.
- **429** carries a JSON body with ``rate_limit``, ``remaining_requests`` and
  ``reset_time`` (``yyyy/MM/dd HH:mm:ss Z``); no Retry-After header is documented.

❓ Not proven live yet: registering an app needs an Artist Pro subscription and
SoundCloud's approval of a request form, so every shape here is the documented one until
the first real call. Field names are read defensively for that reason.
"""
from __future__ import annotations

import base64
import hashlib
import logging
import os
import secrets
import time
import urllib.parse
from typing import Any

import httpx

logger = logging.getLogger(__name__)

API_BASE = "https://api.soundcloud.com"
AUTH_BASE = "https://secure.soundcloud.com"
SITE_BASE = "https://soundcloud.com"
HTTP_TIMEOUT = 30.0
UPLOAD_TIMEOUT = 600.0          # a large WAV over a slow uplink
PAGE_SIZE = 200                 # the documented maximum per page
MAX_PAGES = 50                  # 10 000 tracks — a stop, not a target

# The Library's audio set minus opus, which SoundCloud's list does not carry.
ACCEPTED_AUDIO = ("mp3", "wav", "flac", "ogg", "m4a", "aac", "aiff", "aif", "wma")


class ScAuthError(Exception):
    """The token was refused, or there is none — the operator must re-authorise."""


class ScRateLimited(Exception):
    """HTTP 429. ``reset_time`` is SoundCloud's own string when the body carried one."""

    def __init__(self, message: str, reset_time: str = ""):
        super().__init__(message)
        self.reset_time = reset_time


def _safe_int(v: Any) -> int:
    try:
        return int(v or 0)
    except (TypeError, ValueError):
        return 0


# ── PKCE + the authorize URL (used by routes/sc_api.py) ──────────────────────

def pkce_pair() -> tuple[str, str]:
    """(verifier, S256 challenge), base64url without padding as RFC 7636 wants."""
    verifier = secrets.token_urlsafe(64)
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return verifier, base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def authorize_url(client_id: str, redirect_uri: str, challenge: str, state: str) -> str:
    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "state": state,
    }
    return f"{AUTH_BASE}/authorize?{urllib.parse.urlencode(params)}"


def tag_list(tags: list[str] | None) -> str:
    """SoundCloud's ``tag_list`` is space separated; a tag with a space is quoted."""
    out = []
    for t in tags or []:
        t = str(t or "").strip().strip('"')
        if not t:
            continue
        out.append(f'"{t}"' if " " in t else t)
    return " ".join(out)


class ScClient:
    def __init__(self, client_id: str = "", client_secret: str = "",
                 access_token: str = "", refresh_token: str = "", expires_at: float = 0.0):
        self.client_id = client_id
        self.client_secret = client_secret
        self.access_token = access_token
        self.refresh_token = refresh_token
        # Wall-clock expiry (settings carry it across processes); 0 = unknown → refresh first.
        self.expires_at = float(expires_at or 0.0)
        self._client: httpx.AsyncClient | None = None
        self._me: dict | None = None
        self.tokens_changed = False     # set whenever a refresh rotated the pair

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
        return {"Authorization": f"OAuth {self.access_token}",
                "Accept": "application/json; charset=utf-8"}

    # ── OAuth ────────────────────────────────────────────────────────────────

    async def _token_request(self, data: dict) -> dict:
        r = await self._http().post(f"{AUTH_BASE}/oauth/token", data={
            **data, "client_id": self.client_id, "client_secret": self.client_secret,
        }, headers={"Accept": "application/json; charset=utf-8"})
        try:
            body = r.json()
        except Exception:
            body = {}
        if r.status_code >= 400 or body.get("error"):
            msg = (body.get("error_description") or body.get("error")
                   or body.get("message") or f"HTTP {r.status_code}")
            raise ScAuthError(f"SoundCloud auth failed: {msg}")
        self.access_token = body.get("access_token", "") or self.access_token
        new_refresh = body.get("refresh_token", "")
        if new_refresh:
            self.refresh_token = new_refresh
        # Renew a minute early so a request never straddles the expiry.
        self.expires_at = time.time() + max(60, _safe_int(body.get("expires_in")) - 60)
        self.tokens_changed = True
        return body

    async def exchange_code(self, code: str, redirect_uri: str, verifier: str) -> dict:
        """The authorization-code exchange. The caller persists the pair."""
        return await self._token_request({
            "grant_type": "authorization_code", "redirect_uri": redirect_uri,
            "code_verifier": verifier, "code": code,
        })

    async def refresh(self) -> dict:
        if not self.refresh_token:
            raise ScAuthError("SoundCloud is not authorised — connect it in Settings")
        return await self._token_request({"grant_type": "refresh_token",
                                          "refresh_token": self.refresh_token})

    async def _ensure_token(self) -> None:
        if not self.access_token or time.time() >= self.expires_at:
            await self.refresh()

    async def _request(self, method: str, path: str, *, retry_auth: bool = True, **kw) -> httpx.Response:
        await self._ensure_token()
        url = path if path.startswith("http") else f"{API_BASE}/{path.lstrip('/')}"
        r = await self._http().request(method, url, headers={**self._headers(), **kw.pop("headers", {})}, **kw)
        if r.status_code == 401 and retry_auth:
            await self.refresh()
            return await self._request(method, path, retry_auth=False, **kw)
        if r.status_code == 401:
            raise ScAuthError("SoundCloud rejected the token (401) — re-authorise in Settings")
        if r.status_code == 429:
            try:
                body = r.json()
            except Exception:
                body = {}
            raise ScRateLimited(f"SoundCloud rate limit: {body.get('rate_limit', {}).get('name', 'requests')} "
                                f"until {body.get('reset_time', 'later')}", str(body.get("reset_time", "")))
        return r

    async def _get_json(self, path: str, params: dict | None = None) -> Any:
        r = await self._request("GET", path, params=params)
        if r.status_code >= 400:
            return None
        try:
            return r.json()
        except Exception:
            return None

    # ── The user ─────────────────────────────────────────────────────────────

    async def get_me(self) -> dict | None:
        if self._me is None:
            self._me = await self._get_json("me") or None
        return self._me

    async def validate_session(self) -> str | None:
        """The connected user's handle (``permalink``), or None when nothing is configured."""
        if not (self.refresh_token or self.access_token):
            return None
        me = await self.get_me()
        if not me:
            return None
        return me.get("permalink") or me.get("username") or ""

    async def get_follower_count(self) -> int | None:
        me = await self.get_me()
        if not me or me.get("followers_count") is None:
            return None
        return _safe_int(me.get("followers_count"))

    # ── Tracks ───────────────────────────────────────────────────────────────

    async def get_my_tracks(self) -> list[dict]:
        """Every track of the connected user, paged with ``linked_partitioning``."""
        out: list[dict] = []
        params: dict | None = {"limit": PAGE_SIZE, "linked_partitioning": "true"}
        path = "me/tracks"
        for _ in range(MAX_PAGES):
            data = await self._get_json(path, params)
            if data is None:
                break
            items = data.get("collection") if isinstance(data, dict) else data
            for t in items or []:
                if isinstance(t, dict):
                    out.append(t)
            nxt = data.get("next_href") if isinstance(data, dict) else None
            if not nxt:
                break
            path, params = nxt, None
        return out

    async def get_track(self, track_id: str) -> dict | None:
        return await self._get_json(f"tracks/{urllib.parse.quote(str(track_id), safe='')}")

    @staticmethod
    def parse_track(t: dict) -> dict:
        """A track in the ``sc_submissions`` shape. ``submission_id`` is the numeric id as
        text (the ``urn`` is kept alongside); counts read both the current and the older
        names."""
        tid = t.get("id")
        sid = str(tid) if tid is not None else str(t.get("urn", "")).rsplit(":", 1)[-1]
        user = t.get("user") if isinstance(t.get("user"), dict) else {}
        return {
            "submission_id": sid,
            "urn": str(t.get("urn", "") or ""),
            "title": t.get("title", "") or "",
            "description": t.get("description", "") or "",
            "genre": t.get("genre", "") or "",
            "tag_list": t.get("tag_list", "") or "",
            "sharing": t.get("sharing", "") or "",
            "username": user.get("permalink", "") or user.get("username", "") or "",
            "link": t.get("permalink_url", "") or "",
            "artwork_url": t.get("artwork_url", "") or "",
            "duration_ms": _safe_int(t.get("duration")),
            "posted_at": t.get("created_at", "") or "",
            "views": _safe_int(t.get("playback_count")),
            "favorites_count": _safe_int(t.get("likes_count", t.get("favoritings_count"))),
            "comments_count": _safe_int(t.get("comment_count")),
            "reposts_count": _safe_int(t.get("reposts_count")),
            "downloads_count": _safe_int(t.get("download_count")),
        }

    async def upload_track(self, *, file_path: str, title: str, description: str = "",
                           tags: list[str] | None = None, genre: str = "", sharing: str = "public",
                           artwork_path: str | None = None, artist: str = "") -> dict:
        """``POST /tracks`` multipart. Returns {"success", "id", "url"} or {"success": False, "error"}."""
        if not os.path.isfile(file_path):
            return {"success": False, "error": f"file not found: {file_path}"}
        await self._ensure_token()
        fields = {
            "track[title]": title or os.path.basename(file_path),
            "track[description]": description or "",
            "track[sharing]": "private" if sharing == "private" else "public",
        }
        tl = tag_list(tags)
        if tl:
            fields["track[tag_list]"] = tl
        if genre:
            fields["track[genre]"] = genre
        if artist:
            fields["track[artist]"] = artist
        files: dict[str, Any] = {}
        fh_audio = open(file_path, "rb")
        files["track[asset_data]"] = (os.path.basename(file_path), fh_audio, "application/octet-stream")
        fh_art = None
        if artwork_path and os.path.isfile(artwork_path):
            fh_art = open(artwork_path, "rb")
            files["track[artwork_data]"] = (os.path.basename(artwork_path), fh_art, "image/jpeg")
        try:
            async with httpx.AsyncClient(timeout=UPLOAD_TIMEOUT) as up:
                r = await up.post(f"{API_BASE}/tracks", data=fields, files=files, headers=self._headers())
        finally:
            fh_audio.close()
            if fh_art:
                fh_art.close()
        if r.status_code == 401:
            return {"success": False, "error": "SoundCloud rejected the token (401) — re-authorise in Settings"}
        if r.status_code == 429:
            return {"success": False, "error": "SoundCloud rate limit — try again later"}
        if r.status_code >= 400:
            return {"success": False, "error": f"SoundCloud upload failed (HTTP {r.status_code}): {r.text[:300]}"}
        try:
            body = r.json()
        except Exception:
            body = {}
        parsed = self.parse_track(body if isinstance(body, dict) else {})
        if not parsed["submission_id"]:
            return {"success": False, "error": "upload finished but SoundCloud returned no track id"}
        return {"success": True, "id": parsed["submission_id"], "url": parsed["link"] or f"{SITE_BASE}/", "track": parsed}

    async def update_track(self, track_id: str, *, title: str | None = None, description: str | None = None,
                           tags: list[str] | None = None, genre: str | None = None,
                           sharing: str | None = None, artwork_path: str | None = None) -> dict:
        """``PUT /tracks/{id}`` — metadata (and artwork) only; the audio never changes."""
        fields: dict[str, str] = {}
        if title is not None:
            fields["track[title]"] = title
        if description is not None:
            fields["track[description]"] = description
        if tags is not None:
            fields["track[tag_list]"] = tag_list(tags)
        if genre is not None:
            fields["track[genre]"] = genre
        if sharing is not None:
            fields["track[sharing]"] = "private" if sharing == "private" else "public"
        files: dict[str, Any] = {}
        fh_art = None
        if artwork_path and os.path.isfile(artwork_path):
            fh_art = open(artwork_path, "rb")
            files["track[artwork_data]"] = (os.path.basename(artwork_path), fh_art, "image/jpeg")
        try:
            r = await self._request("PUT", f"tracks/{urllib.parse.quote(str(track_id), safe='')}",
                                    data=fields, files=files or None)
        finally:
            if fh_art:
                fh_art.close()
        if r.status_code >= 400:
            return {"success": False, "error": f"SoundCloud update failed (HTTP {r.status_code}): {r.text[:300]}"}
        try:
            parsed = self.parse_track(r.json())
        except Exception:
            parsed = {"submission_id": str(track_id), "link": ""}
        return {"success": True, "id": parsed.get("submission_id") or str(track_id), "url": parsed.get("link", "")}
