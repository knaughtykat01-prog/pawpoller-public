"""Twitch (ttv) Helix client — channel tracking + go-live (spec 045).

Twitch is watched, never posted to. Every Helix call needs the install's own Twitch app
(a client id + secret registered free at dev.twitch.tv) and a bearer token:

  * an APP token (client-credentials grant, no sign-in) reads anything public by id or name:
      GET /users?login=        the channel: permanent id, display name
      GET /streams?user_id=    live now? title, game, viewers, started_at, mature (up to 100 ids per call)
      GET /videos?type=archive past broadcasts      GET /clips   clips
      POST/GET/DELETE /eventsub/subscriptions   the go-live webhook (stream.online / stream.offline)
  * the streamer's USER token (Connect, authorization-code grant) adds what only they may see:
      GET /channels/followers  (moderator:read:followers)   GET /subscriptions (channel:read:subscriptions)
      GET /bits/leaderboard    (bits:read)

Lifetime channel views (``view_count`` on /users) are deprecated by Twitch and never read.
Twitch's ids are the identity: a renamed channel keeps its user id (spec FR-001).
"""
from __future__ import annotations

import logging
import re
import time
from typing import Any
from urllib.parse import urlencode

import httpx

logger = logging.getLogger(__name__)

HELIX = "https://api.twitch.tv/helix"
ID_BASE = "https://id.twitch.tv/oauth2"
SITE = "https://www.twitch.tv"
HTTP_TIMEOUT = 20.0
USER_SCOPES = ("moderator:read:followers", "channel:read:subscriptions", "bits:read")

# Twitch logins: 4–25 letters, digits, underscore (older accounts may be shorter).
_LOGIN = re.compile(r"^[A-Za-z0-9_]{1,25}$")
_DURATION = re.compile(r"(?:(\d+)h)?(?:(\d+)m)?(?:(\d+)s)?$")

# The app token per client id, shared by every client in this process: one token serves
# every channel, and asking for a new one per poll would be wasteful. {client_id: (token, expires_at)}
_APP_TOKENS: dict[str, tuple[str, float]] = {}


class TtvError(Exception):
    """Twitch answered, but not with what we asked for."""


class TtvAuthError(TtvError):
    """The app credentials or the user token were refused."""


def clean_login(raw: str) -> str:
    """A channel login from what someone typed — tolerates a pasted twitch.tv address or a leading @."""
    s = (raw or "").strip().rstrip("/")
    if "twitch.tv/" in s:
        s = s.split("twitch.tv/", 1)[1].split("/", 1)[0].split("?", 1)[0]
    s = s.lstrip("@").lower()
    return s if _LOGIN.match(s) else ""


def _int(v: Any) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return 0


def _ts(v: Any) -> str | None:
    """'2026-10-18T09:00:00Z' → '2026-10-18 09:00:00', the shape every table stores."""
    s = str(v or "")
    return s.replace("T", " ").rstrip("Z")[:19] if s else None


def duration_seconds(raw: Any) -> int:
    """Twitch video durations read '3h2m1s'; clips give seconds as a number."""
    if isinstance(raw, (int, float)):
        return int(raw)
    m = _DURATION.match(str(raw or "").strip())
    if not m or not any(m.groups()):
        return 0
    h, mi, s = (int(x or 0) for x in m.groups())
    return h * 3600 + mi * 60 + s


def _thumb(url: str, w: int = 640, h: int = 360) -> str:
    return (url or "").replace("%{width}", str(w)).replace("%{height}", str(h)) \
        .replace("{width}", str(w)).replace("{height}", str(h))


def parse_stream(d: dict) -> dict:
    """A live stream → the fields go-live and the dashboard use."""
    login = str(d.get("user_login") or "")
    return {
        "stream_key": str(d.get("id") or ""),
        "user_id": str(d.get("user_id") or ""),
        "login": login,
        "name": str(d.get("user_name") or login),
        "title": str(d.get("title") or ""),
        "game": str(d.get("game_name") or ""),
        "viewers": _int(d.get("viewer_count")),
        "started_at": _ts(d.get("started_at")),
        "mature": bool(d.get("is_mature")),
        "link": f"{SITE}/{login}" if login else "",
    }


def parse_video(v: dict, login: str) -> dict:
    """A past broadcast → a ttv_submissions row (id 'v<id>')."""
    return {
        "submission_id": f"v{v.get('id')}",
        "kind": "vod",
        "title": str(v.get("title") or "Untitled broadcast"),
        "username": login,
        "link": str(v.get("url") or ""),
        "thumbnail_url": _thumb(str(v.get("thumbnail_url") or "")),
        "duration_s": duration_seconds(v.get("duration")),
        "views": _int(v.get("view_count")),
        "posted_at": _ts(v.get("created_at")),
        "stream_id": str(v.get("stream_id") or ""),
    }


def parse_clip(c: dict, login: str) -> dict:
    """A clip → a ttv_submissions row (id 'c<id>')."""
    return {
        "submission_id": f"c{c.get('id')}",
        "kind": "clip",
        "title": str(c.get("title") or "Untitled clip"),
        "username": login,
        "link": str(c.get("url") or ""),
        "thumbnail_url": str(c.get("thumbnail_url") or ""),
        "duration_s": duration_seconds(c.get("duration")),
        "views": _int(c.get("view_count")),
        "posted_at": _ts(c.get("created_at")),
        "stream_id": "",
    }


def authorize_url(client_id: str, redirect_uri: str, state: str) -> str:
    return f"{ID_BASE}/authorize?" + urlencode({
        "response_type": "code", "client_id": client_id, "redirect_uri": redirect_uri,
        "scope": " ".join(USER_SCOPES), "state": state, "force_verify": "true",
    })


class TtvClient:
    def __init__(self, client_id: str = "", client_secret: str = "", *, login: str = "", user_id: str = "",
                 access_token: str = "", refresh_token: str = "", token_expires_at: float = 0):
        self.client_id = (client_id or "").strip()
        self.client_secret = (client_secret or "").strip()
        self.login = clean_login(login)
        self.user_id = str(user_id or "")
        self.access_token = access_token or ""
        self.refresh_token = refresh_token or ""
        self.token_expires_at = float(token_expires_at or 0)
        self.tokens_changed = False          # the poller saves a renewed user token when this is set
        self._client: httpx.AsyncClient | None = None

    # ── plumbing ──────────────────────────────────────────────────────────

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=HTTP_TIMEOUT)
        return self._client

    @property
    def has_app(self) -> bool:
        return bool(self.client_id and self.client_secret)

    @property
    def connected(self) -> bool:
        return bool(self.refresh_token or self.access_token)

    async def app_token(self, renew: bool = False) -> str:
        if not self.has_app:
            raise TtvAuthError("Add your Twitch app's client id and secret in Settings → Platforms → Twitch.")
        cached = _APP_TOKENS.get(self.client_id)
        if cached and not renew and cached[1] > time.time() + 60:
            return cached[0]
        r = await self._http().post(f"{ID_BASE}/token", data={
            "client_id": self.client_id, "client_secret": self.client_secret, "grant_type": "client_credentials"})
        if r.status_code in (400, 401, 403):
            _APP_TOKENS.pop(self.client_id, None)
            raise TtvAuthError("Twitch refused the app's client id or secret — check them in Settings.")
        if r.status_code != 200:
            raise TtvError(f"Twitch answered HTTP {r.status_code} when asked for an app token.")
        d = r.json()
        tok = str(d.get("access_token") or "")
        _APP_TOKENS[self.client_id] = (tok, time.time() + _int(d.get("expires_in")))
        return tok

    async def _user_token(self) -> str:
        if not self.connected:
            raise TtvAuthError("Twitch isn't connected for this channel.")
        if self.access_token and self.token_expires_at > time.time() + 60:
            return self.access_token
        if not self.refresh_token:
            return self.access_token
        await self.refresh()
        return self.access_token

    async def refresh(self) -> None:
        r = await self._http().post(f"{ID_BASE}/token", data={
            "client_id": self.client_id, "client_secret": self.client_secret,
            "grant_type": "refresh_token", "refresh_token": self.refresh_token})
        if r.status_code in (400, 401):
            raise TtvAuthError("Twitch no longer accepts this connection — connect Twitch again in Settings.")
        if r.status_code != 200:
            raise TtvError(f"Twitch answered HTTP {r.status_code} when renewing the connection.")
        self._take_tokens(r.json())

    def _take_tokens(self, d: dict) -> None:
        self.access_token = str(d.get("access_token") or "")
        self.refresh_token = str(d.get("refresh_token") or self.refresh_token)
        self.token_expires_at = time.time() + _int(d.get("expires_in"))
        self.tokens_changed = True

    async def exchange_code(self, code: str, redirect_uri: str) -> None:
        r = await self._http().post(f"{ID_BASE}/token", data={
            "client_id": self.client_id, "client_secret": self.client_secret, "code": code,
            "grant_type": "authorization_code", "redirect_uri": redirect_uri})
        if r.status_code != 200:
            raise TtvAuthError(f"Twitch refused the approval (HTTP {r.status_code}).")
        self._take_tokens(r.json())

    async def validate_user(self) -> dict | None:
        """{login, user_id, scopes} of whoever approved the user token, or None."""
        tok = await self._user_token()
        r = await self._http().get(f"{ID_BASE}/validate", headers={"Authorization": f"OAuth {tok}"})
        if r.status_code != 200:
            return None
        d = r.json()
        return {"login": str(d.get("login") or ""), "user_id": str(d.get("user_id") or ""),
                "scopes": list(d.get("scopes") or [])}

    async def _helix(self, method: str, path: str, *, params=None, json=None, user: bool = False) -> Any:
        for attempt in (0, 1):
            tok = await (self._user_token() if user else self.app_token(renew=attempt == 1))
            r = await self._http().request(method, f"{HELIX}{path}", params=params, json=json,
                                           headers={"Client-Id": self.client_id, "Authorization": f"Bearer {tok}"})
            if r.status_code == 401 and attempt == 0:
                if user:
                    if not self.refresh_token:
                        break
                    await self.refresh()
                continue                       # an app token renews on the second pass
            break
        if r.status_code == 401:
            raise TtvAuthError("Twitch refused the sign-in — connect Twitch again in Settings." if user
                               else "Twitch refused the app's credentials — check them in Settings.")
        if r.status_code == 403:
            raise TtvAuthError("Twitch says this connection isn't allowed to read that — connect Twitch again.")
        if r.status_code == 429:
            raise TtvError("Twitch is rate-limiting requests right now — the next check will try again.")
        if r.status_code == 204:
            return {}
        if r.status_code >= 400:
            raise TtvError(f"Twitch answered HTTP {r.status_code}.")
        return r.json()

    # ── reads with the app token ──────────────────────────────────────────

    async def get_user(self, login: str | None = None) -> dict | None:
        """The channel by login (or by the stored id): {user_id, login, name, avatar}, or None."""
        params = {"login": clean_login(login)} if login else ({"id": self.user_id} if self.user_id
                                                              else {"login": self.login})
        if not any(params.values()):
            return None
        d = await self._helix("GET", "/users", params=params)
        rows = (d or {}).get("data") or []
        if not rows:
            return None
        u = rows[0]
        return {"user_id": str(u.get("id") or ""), "login": str(u.get("login") or ""),
                "name": str(u.get("display_name") or u.get("login") or ""),
                "avatar": str(u.get("profile_image_url") or "")}

    async def validate_session(self) -> str | None:
        """The channel's login as Twitch spells it, or None. (Named like every client's check.)"""
        u = await self.get_user()
        return u["login"] if u else None

    async def get_streams(self, user_ids: list[str]) -> dict[str, dict]:
        """{user_id: stream} for every id that is live now (one request per 100 ids)."""
        out: dict[str, dict] = {}
        ids = [i for i in dict.fromkeys(user_ids) if i]
        for k in range(0, len(ids), 100):
            d = await self._helix("GET", "/streams", params=[("user_id", i) for i in ids[k:k + 100]]
                                  + [("first", "100")])
            for s in (d or {}).get("data") or []:
                if s.get("type", "live") == "live":
                    st = parse_stream(s)
                    out[st["user_id"]] = st
        return out

    async def get_videos(self) -> list[dict]:
        if not self.user_id:
            return []
        d = await self._helix("GET", "/videos", params={"user_id": self.user_id, "type": "archive", "first": "100"})
        return [parse_video(v, self.login) for v in (d or {}).get("data") or [] if v.get("id")]

    async def get_clips(self) -> list[dict]:
        if not self.user_id:
            return []
        d = await self._helix("GET", "/clips", params={"broadcaster_id": self.user_id, "first": "100"})
        return [parse_clip(c, self.login) for c in (d or {}).get("data") or [] if c.get("id")]

    # ── reads with the streamer's own connection ──────────────────────────

    async def get_follower_count(self) -> int | None:
        """For polling/followers.capture_followers. Needs Connect; None without it."""
        if not (self.connected and self.user_id):
            return None
        d = await self._helix("GET", "/channels/followers", params={"broadcaster_id": self.user_id, "first": "1"},
                              user=True)
        return _int((d or {}).get("total"))

    async def get_subscriptions(self) -> tuple[int, int]:
        """(subscriber count, sub points)."""
        d = await self._helix("GET", "/subscriptions", params={"broadcaster_id": self.user_id, "first": "1"},
                              user=True)
        return _int((d or {}).get("total")), _int((d or {}).get("points"))

    async def get_bits_top(self) -> int:
        """Bits from the channel's top 100 cheerers, all time — the only Bits total Twitch gives."""
        d = await self._helix("GET", "/bits/leaderboard", params={"count": "100", "period": "all"}, user=True)
        return sum(_int(r.get("score")) for r in (d or {}).get("data") or [])

    # ── EventSub webhook subscriptions (app token) ────────────────────────

    async def list_subscriptions(self) -> list[dict]:
        d = await self._helix("GET", "/eventsub/subscriptions")
        return [s for s in (d or {}).get("data") or [] if (s.get("transport") or {}).get("method") == "webhook"]

    async def subscribe(self, kind: str, user_id: str, callback: str, secret: str) -> dict:
        d = await self._helix("POST", "/eventsub/subscriptions", json={
            "type": kind, "version": "1", "condition": {"broadcaster_user_id": user_id},
            "transport": {"method": "webhook", "callback": callback, "secret": secret}})
        return ((d or {}).get("data") or [{}])[0]

    async def unsubscribe(self, sub_id: str) -> None:
        await self._helix("DELETE", "/eventsub/subscriptions", params={"id": sub_id})
