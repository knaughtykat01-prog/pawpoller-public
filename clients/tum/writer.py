"""Tumblr posting client — NPF posts, edits and the OAuth 2 sign-in (spec 024, 4.60.0).

Kept apart from ``client.py`` (polling, API key only). Every write is signed one of two ways:

- **OAuth 2** (the Connect button): ``Authorization: Bearer <access token>``. Tokens last 42 minutes;
  ``refresh()`` swaps the refresh token for a new pair (store both — rotation is not documented either way).
- **OAuth 1** (four values pasted by hand, the pre-4.60 setup): the existing RFC 5849 signer. For a
  multipart or JSON body only the OAuth parameters (and any query string) are signed.

Posts use Tumblr's Neue Post Format: ``POST /v2/blog/{blog}/posts`` as multipart, a ``json`` part with the
content blocks and one part per uploaded file, referenced by ``identifier``. Errors come back as
``TumError`` carrying Tumblr's subcode and a plain-words sentence (research R2).
"""
from __future__ import annotations

import json
import mimetypes
import os
import secrets
import time
from typing import Any
from urllib.parse import urlencode

import httpx

from clients.tum.client import _API_BASE, _HEADERS, _normalise_blog, _oauth1_header

AUTHORIZE_URL = "https://www.tumblr.com/oauth2/authorize"
TOKEN_URL = f"{_API_BASE}/oauth2/token"
SCOPES = "basic write offline_access"

# Tumblr's documented subcodes (api.md "Rate Limits" / errors) in plain words.
_SUBCODES = {
    8004: "Tumblr's daily image upload limit is used up — try again tomorrow",
    8011: "Tumblr's daily video limit (20 videos or 60 minutes) is used up — try again tomorrow, or schedule it",
    8023: "Tumblr's daily post limit (250 posts) is used up — try again tomorrow",
    8005: "Tumblr didn't accept that file's format",
    8010: "Tumblr is still processing this video — try again in a few minutes",
}


class TumError(RuntimeError):
    def __init__(self, words: str, status: int = 0, subcode: int = 0):
        super().__init__(words)
        self.status, self.subcode = status, subcode


def plain_error(status: int, body: Any) -> TumError:
    errs = (body or {}).get("errors") if isinstance(body, dict) else None
    sub = 0
    detail = ""
    if isinstance(errs, list) and errs:
        e = errs[0] if isinstance(errs[0], dict) else {}
        try:
            sub = int(e.get("code") or 0)
        except (TypeError, ValueError):
            sub = 0
        detail = str(e.get("detail") or e.get("title") or "")
    if sub in _SUBCODES:
        return TumError(_SUBCODES[sub], status, sub)
    if status == 401:
        return TumError("Tumblr sign-in expired; press Connect again (Settings → Accounts → Tumblr)", status, sub)
    if status == 403 and not detail:
        return TumError("Tumblr refused: this sign-in can't post to that blog", status, sub)
    if status == 413:
        return TumError("That file is too big for Tumblr", status, sub)
    if status == 429:
        return TumError("Tumblr says too many requests right now — try again later", status, sub)
    msg = detail or str(((body or {}).get("meta") or {}).get("msg") or "") if isinstance(body, dict) else ""
    return TumError(f"Tumblr refused the post ({status}{f': {msg}' if msg else ''})", status, sub)


class TumWriter:
    """One Tumblr blog, signed with OAuth 2 (preferred) or the pasted OAuth 1 set."""

    def __init__(self, blog: str, *, access_token: str = "", consumer_key: str = "", consumer_secret: str = "",
                 oauth_token: str = "", oauth_token_secret: str = "",
                 transport: httpx.AsyncBaseTransport | None = None):
        self.blog = _normalise_blog(blog)
        self.access_token = (access_token or "").strip()
        self._ck, self._cs = consumer_key, consumer_secret
        self._ot, self._ots = oauth_token, oauth_token_secret
        self._http = httpx.AsyncClient(timeout=httpx.Timeout(60.0, read=600.0), headers=_HEADERS,
                                       transport=transport or httpx.AsyncHTTPTransport(retries=2))

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        await self.close()

    async def close(self) -> None:
        await self._http.aclose()

    @property
    def can_post(self) -> bool:
        return bool(self.blog and (self.access_token or (self._ck and self._cs and self._ot and self._ots)))

    def _auth(self, method: str, url: str, query: dict | None = None) -> dict:
        if self.access_token:
            return {"Authorization": f"Bearer {self.access_token}"}
        if not (self._ck and self._cs and self._ot and self._ots):
            raise TumError("Tumblr isn't connected for posting — press Connect (Settings → Accounts → Tumblr)")
        return {"Authorization": _oauth1_header(method, url, dict(query or {}), consumer_key=self._ck,
                                                consumer_secret=self._cs, token=self._ot, token_secret=self._ots,
                                                timestamp=int(time.time()), nonce=secrets.token_hex(16))}

    async def _call(self, method: str, path: str, *, query: dict | None = None, **kw) -> dict:
        url = f"{_API_BASE}{path}"
        try:
            resp = await self._http.request(method, url, params=query, headers=self._auth(method, url, query), **kw)
        except httpx.HTTPError as e:
            raise TumError(f"Couldn't reach Tumblr: {type(e).__name__}") from e
        try:
            body = resp.json() if resp.content else {}
        except ValueError:
            body = {}
        if resp.status_code >= 400:
            raise plain_error(resp.status_code, body)
        return (body or {}).get("response") or {} if isinstance(body, dict) else {}

    def post_url(self, post_id: str) -> str:
        host = self.blog if "." in self.blog else f"{self.blog}.tumblr.com"
        return f"https://{host}/post/{post_id}" if post_id else ""

    # -- posts -------------------------------------------------------------------------

    async def create_post(self, content: list[dict], *, tags: list[str] | None = None, state: str = "published",
                          label: bool = False, categories: list[str] | None = None,
                          files: dict[str, str] | None = None) -> dict:
        """NPF create. ``files`` = {identifier: path} referenced by the content's media blocks.
        → {"id", "state", "url"}."""
        params = _post_params(content, tags, state, label, categories)
        parts: list = [("json", (None, json.dumps(params), "application/json"))]
        handles = []
        try:
            for ident, path in (files or {}).items():
                fh = open(path, "rb")
                handles.append(fh)
                parts.append((ident, (os.path.basename(path), fh, _mime(path))))
            r = await self._call("POST", f"/blog/{self.blog}/posts", files=parts)
        finally:
            for fh in handles:
                fh.close()
        pid = str(r.get("id") or r.get("id_string") or "")
        return {"id": pid, "state": str(r.get("state") or state), "url": self.post_url(pid)}

    async def get_post(self, post_id: str) -> dict:
        """The post in NPF (content, tags, state, labels)."""
        return await self._call("GET", f"/blog/{self.blog}/posts/{post_id}", query={"post_format": "npf"})

    async def edit_post(self, post_id: str, content: list[dict], *, tags: list[str] | None = None,
                        state: str | None = None, label: bool = False, categories: list[str] | None = None,
                        files: dict[str, str] | None = None) -> dict:
        params = _post_params(content, tags, state, label, categories)
        if files:
            parts: list = [("json", (None, json.dumps(params), "application/json"))]
            handles = []
            try:
                for ident, path in files.items():
                    fh = open(path, "rb")
                    handles.append(fh)
                    parts.append((ident, (os.path.basename(path), fh, _mime(path))))
                r = await self._call("PUT", f"/blog/{self.blog}/posts/{post_id}", files=parts)
            finally:
                for fh in handles:
                    fh.close()
        else:
            r = await self._call("PUT", f"/blog/{self.blog}/posts/{post_id}", json=params)
        pid = str(r.get("id") or r.get("id_string") or post_id)
        return {"id": pid, "state": str(r.get("state") or state or ""), "url": self.post_url(pid)}

    async def recent_posts(self, limit: int = 5) -> list[dict]:
        """The blog's newest posts in NPF (any state the sign-in can see)."""
        r = await self._call("GET", f"/blog/{self.blog}/posts", query={"limit": limit, "npf": "true"})
        return [p for p in r.get("posts") or [] if isinstance(p, dict)]

    async def user_info(self) -> dict:
        """→ {"name": user, "blogs": [blog names]} for the signed-in user."""
        r = await self._call("GET", "/user/info")
        user = r.get("user") or {}
        return {"name": str(user.get("name") or ""),
                "blogs": [str(b.get("name") or "") for b in user.get("blogs") or [] if isinstance(b, dict)]}


def _post_params(content, tags, state, label, categories) -> dict:
    params: dict = {"content": content, "has_community_label": bool(label)}
    if label:
        params["community_label_categories"] = list(categories or [])
    if tags is not None:
        params["tags"] = ",".join(t.replace(",", " ") for t in tags)
    if state:
        params["state"] = state
    return params


def _mime(path: str) -> str:
    return mimetypes.guess_type(path)[0] or "application/octet-stream"


def labelled(post: dict) -> bool:
    """Did Tumblr keep a Mature label on this post? Any shape Tumblr uses counts (research R3)."""
    if not isinstance(post, dict):
        return False
    if post.get("has_community_label") or post.get("hasCommunityLabel"):
        return True
    cl = post.get("community_labels") or post.get("communityLabels") or {}
    if isinstance(cl, dict):
        for key in ("has_community_label", "hasCommunityLabel"):
            if key in cl:                       # an explicit answer wins over leftover categories
                return bool(cl[key])
        return bool(cl.get("categories"))
    return False


# -- OAuth 2 (the Connect button) ------------------------------------------------------

def authorize_url(client_id: str, redirect_uri: str, state: str) -> str:
    return AUTHORIZE_URL + "?" + urlencode({"client_id": client_id, "response_type": "code", "scope": SCOPES,
                                            "state": state, "redirect_uri": redirect_uri})


async def _token(data: dict, transport: httpx.AsyncBaseTransport | None = None) -> dict:
    async with httpx.AsyncClient(timeout=30.0, headers=_HEADERS, transport=transport) as http:
        try:
            resp = await http.post(TOKEN_URL, data=data)
        except httpx.HTTPError as e:
            raise TumError(f"Couldn't reach Tumblr: {type(e).__name__}") from e
    try:
        body = resp.json()
    except ValueError:
        body = {}
    if resp.status_code >= 400 or not body.get("access_token"):
        err = str(body.get("error_description") or body.get("error") or resp.status_code)
        raise TumError(f"Tumblr didn't accept the sign-in ({err}) — press Connect again", resp.status_code)
    return {"tum_oauth2_access_token": str(body["access_token"]),
            "tum_oauth2_refresh_token": str(body.get("refresh_token") or data.get("refresh_token") or ""),
            "tum_oauth2_expires_at": str(int(time.time()) + int(body.get("expires_in") or 2520))}


async def exchange_code(code: str, client_id: str, client_secret: str, redirect_uri: str,
                        transport: httpx.AsyncBaseTransport | None = None) -> dict:
    return await _token({"grant_type": "authorization_code", "code": code, "client_id": client_id,
                         "client_secret": client_secret, "redirect_uri": redirect_uri}, transport)


async def refresh(refresh_token: str, client_id: str, client_secret: str,
                  transport: httpx.AsyncBaseTransport | None = None) -> dict:
    return await _token({"grant_type": "refresh_token", "refresh_token": refresh_token, "client_id": client_id,
                         "client_secret": client_secret}, transport)
