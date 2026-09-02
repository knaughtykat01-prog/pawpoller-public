"""DeviantArt (DA) HTTP client.

Polling uses the **official OAuth2 API** (as of 2.47.0): a client-credentials
token (client_id + client_secret, no user login) enumerates the target user's
gallery via ``/gallery/all`` and pulls per-deviation stats via
``/deviation/metadata?ext_stats=true`` — which returns views, views_today,
favourites, comments, downloads and downloads_today for ANY public deviation.
This replaces the old browser-cookie Eclipse ``_napi`` scrape, which needed a
pasted cookie (that expired) and the Cloudflare Worker proxy (DA IP-blocks
datacenter IPs on the *frontend*; the *API* is not blocked). See
``docs/research/deviantart_official_api.md``.

The legacy cookie/``_napi`` methods are retained as a fallback: if no
client_id/client_secret is configured but a ``da_cookie`` is, the client falls
back to the old scrape. Posting already uses the official OAuth2 API.

Key details:
  - Deviation IDs stored in the DB are integers (parsed from the deviation URL);
    the API's UUID ``deviationid`` is used only transiently for metadata calls.
  - Stats: views, favourites, comments, downloads
  - Auth: client-credentials OAuth (primary) or browser cookie (fallback)
  - ext_stats caps metadata at 10 deviations per call; respectful delays apply
"""

from __future__ import annotations
import asyncio
import html
import json
import logging
import re
import time
from datetime import datetime, timezone
from html import unescape
from typing import Any

import httpx

import config

logger = logging.getLogger(__name__)

_BASE = "https://www.deviantart.com"
_API = "https://www.deviantart.com/api/v1/oauth2"  # official OAuth2 API base
_TOKEN_URL = "https://www.deviantart.com/oauth2/token"  # NOT under /api/v1 — that 404s
_META_BATCH = 10  # deviation/metadata caps at 10 deviations/call when ext_stats=true

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/html, */*",
    "Accept-Language": "en-US,en;q=0.5",
    "Referer": "https://www.deviantart.com/",
}


_DA_TAG_BAD = re.compile(r"[^A-Za-z0-9_]+")


def sanitize_tags(tags: list[str] | None) -> tuple[list[str], list[str]]:
    """Coerce tags into what DeviantArt accepts. Returns (kept, dropped).

    DA's own schema says tags are "Letters, numbers and underscore only" —
    stricter than FurryNetwork's, which also allows `-`. The catalogue is tagged
    booru-style, so `kii_(secondfur)` and `oral_(sex)` are ordinary tags that
    FA, Inkbunny and e621 all accept.

    Illegal characters become underscores rather than being deleted, so
    `kii_(secondfur)` survives as `kii_secondfur` instead of collapsing into
    the meaningless `kiisecondfur` — the same reasoning, and the same shape, as
    `clients/fn/client.sanitize_tags`.
    """
    kept: list[str] = []
    dropped: list[str] = []
    seen: set[str] = set()
    for raw in tags or []:
        clean = _DA_TAG_BAD.sub("_", str(raw)).strip("_")
        clean = re.sub(r"_{2,}", "_", clean)
        # A one-character remainder is junk, not a tag: `<3` reduces to `3`,
        # which DA would accept and which means nothing on its own. Two is the
        # floor rather than FN's three so that real short tags (`69`) survive.
        if len(clean) < 2:
            dropped.append(str(raw))
            continue
        if clean.lower() in seen:
            continue
        seen.add(clean.lower())
        kept.append(clean)
    return kept, dropped


# ── Descriptions: the format DeviantArt's own editor speaks ──────────────────
#
# ⚠ DA stores a description as a **structured document**, not free text. Its
# editor deserialises what is stored into paragraph nodes; plain text with bare
# newlines has no block elements to map onto, so the editor refuses to open it
# with "Invalid Input — some content in your document cannot be processed by the
# editor", offering only a lossy "Attempt Recovery". Observed live on a real
# deviation, 2026-09-02, on a description PawPoller had posted.
#
# The text still RENDERS on the deviation page — this is not a display bug, and
# an earlier reading of this session that claimed otherwise was a bad DOM query.
# What breaks is HAND-EDITING on DA: the owner cannot open their own description.
#
# Two shapes matter, and they are the same document seen from two sides:
#   • On the page, a paragraph renders as
#       <p style="text-align: left; margin-inline-start: 0px;">…</p>
#   • Over the wire to the editor's save endpoint, that same paragraph is
#       {"type":"paragraph",
#        "attrs":{"indentType":null,"indentation":"","textAlign":"left"},
#        "content":[{"type":"text","text":"…"}]}
#     and a BLANK line is the same node with no "content" key at all.
_DA_P_STYLE = "text-align: left; margin-inline-start: 0px;"


def _split_paragraphs(text: str) -> list[str]:
    """Text -> paragraphs, preserving blank lines as empty entries.

    A run of blank lines collapses to ONE empty paragraph, which is what the
    editor produces when you press Enter twice.
    """
    out: list[str] = []
    blank = False
    for raw in (text or "").replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        line = raw.strip()
        if line:
            out.append(line)
            blank = False
        elif not blank and out:
            out.append("")
            blank = True
    while out and out[-1] == "":
        out.pop()
    return out


def description_to_da_html(text: str) -> str:
    """Render a plain-text description as the paragraph HTML DA stores.

    Used at POST time (``artist_comments``) so a new deviation's description is
    made of real block elements and the editor can open it, instead of the bare
    newlines that produce "Invalid Input".
    """
    paras = _split_paragraphs(text)
    if not paras:
        return ""
    return "".join(
        f'<p style="{_DA_P_STYLE}">{html.escape(p) if p else "<br>"}</p>'
        for p in paras
    )


def description_to_editor_raw(text: str) -> str:
    """Render a plain-text description as the editor's ``editorRaw`` payload.

    Returns the DOUBLE-encoded JSON string the endpoint wants: the value of
    ``editorRaw`` is itself a JSON document, not an object.
    """
    content = []
    for p in _split_paragraphs(text):
        node: dict[str, Any] = {
            "type": "paragraph",
            "attrs": {"indentType": None, "indentation": "", "textAlign": "left"},
        }
        if p:
            node["content"] = [{"type": "text", "text": p}]
        content.append(node)
    return json.dumps({"version": "1", "document": {"type": "doc", "content": content}},
                      ensure_ascii=False)


class DAClient:
    """Async HTTP client for DeviantArt.

    Polling (primary): the official OAuth2 API. A client-credentials token
    (``client_id`` + ``client_secret``, no user login) drives ``/gallery/all``
    for enumeration and ``/deviation/metadata?ext_stats=true`` for stats — which
    returns views/favourites/comments/downloads for any public deviation. Works
    from datacenter IPs, so no CF Worker proxy is needed.

    Polling (fallback): the legacy Eclipse ``_napi`` scrape, used only when no
    ``client_id``/``client_secret`` is configured but a ``cookie_value`` is. That
    path still needs the CF proxy on datacenter IPs.

    Posting: the official OAuth2 API (see the ``oauth_*`` methods below).
    """

    def __init__(self, cookie_value: str = "", target_user: str = "", *,
                 client_id: str = "", client_secret: str = "",
                 proxy_url: str = "", proxy_key: str = "", cookie: str = ""):
        self.cookie_value = cookie_value or cookie  # full cookie string (fallback path)
        self.target_user = target_user
        self.client_id = client_id
        self.client_secret = client_secret

        # Client-credentials app token cache (official API path).
        self._app_token: str = ""
        self._app_token_expires_at: float = 0.0
        # int deviation_id -> {uuid, title, url, thumbnail_url, posted_at, is_mature, username}
        # populated by the OAuth enumeration so details can reuse it without re-fetching.
        self._gallery_cache: dict[int, dict] = {}

        # Use Cloudflare Worker proxy if configured (bypasses datacenter IP blocks).
        # Only relevant to the legacy cookie/_napi path; the official API is not
        # IP-walled, so the poller no longer passes proxy creds for DA.
        if proxy_url and proxy_key:
            from polling.cf_proxy import CloudflareProxyTransport
            transport = CloudflareProxyTransport(proxy_url, proxy_key)
            logger.info("DA client using CF proxy: %s", proxy_url)
        else:
            transport = httpx.AsyncHTTPTransport(retries=2)

        self._http = httpx.AsyncClient(
            timeout=30.0,
            follow_redirects=True,
            headers=_HEADERS,
            transport=transport,
        )
        self._update_cookies()

    @property
    def _use_oauth(self) -> bool:
        """Official API when app creds are present; else legacy cookie scrape."""
        return bool(self.client_id and self.client_secret)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        await self.close()

    def _update_cookies(self) -> None:
        """Parse and set cookies on the HTTP client."""
        # Cookie value is the raw cookie string from browser
        # Parse individual cookies from the string
        cookies = {}
        if self.cookie_value:
            for part in self.cookie_value.split(";"):
                part = part.strip()
                if "=" in part:
                    key, val = part.split("=", 1)
                    cookies[key.strip()] = val.strip()
        self._http.cookies.update(cookies)

    def update_credentials(self, cookie_value: str = "", target_user: str = "", *,
                           client_id: str = "", client_secret: str = "",
                           cookie: str = "") -> None:
        """Re-point the persistent client at another account's credentials."""
        self.cookie_value = cookie_value or cookie
        self.target_user = target_user
        # If the app credentials changed, drop the cached token so the next call
        # mints a fresh one for the new app.
        if (client_id, client_secret) != (self.client_id, self.client_secret):
            self._app_token = ""
            self._app_token_expires_at = 0.0
        self.client_id = client_id
        self.client_secret = client_secret
        self._update_cookies()

    async def close(self) -> None:
        await self._http.aclose()

    # ── Page/API Fetching ─────────────────────────────────────

    async def _get_json(self, url: str, params: dict | None = None) -> dict | None:
        """Fetch a JSON endpoint, handling errors gracefully."""
        try:
            resp = await self._http.get(url, params=params)
            if resp.status_code == 403:
                logger.error("DA: Access denied (403) for %s", url)
                return None
            if resp.status_code == 429:
                logger.warning("DA: Rate limited (429), waiting 30s...")
                await asyncio.sleep(30)
                resp = await self._http.get(url, params=params)
            resp.raise_for_status()
            return resp.json()
        except httpx.HTTPError as e:
            logger.error("DA: Failed to fetch %s: %s", url, e)
            return None
        except Exception as e:
            logger.error("DA: JSON parse error for %s: %s", url, e)
            return None

    async def _get_page(self, url: str) -> str | None:
        """Fetch an HTML page."""
        try:
            resp = await self._http.get(url)
            if resp.status_code in (403, 429):
                if resp.status_code == 429:
                    await asyncio.sleep(30)
                    resp = await self._http.get(url)
                else:
                    return None
            resp.raise_for_status()
            return resp.text
        except httpx.HTTPError as e:
            logger.error("DA: Failed to fetch %s: %s", url, e)
            return None

    # ── Authentication ────────────────────────────────────────

    # ── Official API (OAuth2) — primary polling path ──────────

    async def _get_app_token(self) -> str:
        """Return a cached client-credentials access token, minting it as needed."""
        now = time.time()
        if self._app_token and now < self._app_token_expires_at:
            return self._app_token
        resp = await self._http.post(
            _TOKEN_URL,
            data={
                "grant_type": "client_credentials",
                "client_id": self.client_id,
                "client_secret": self.client_secret,
            },
            timeout=15.0,
        )
        if resp.status_code != 200:
            raise RuntimeError(
                f"DA: client-credentials token failed — {resp.status_code}: {resp.text[:200]}"
            )
        data = resp.json()
        token = data.get("access_token", "")
        if not token:
            raise RuntimeError(f"DA: token response missing access_token: {str(data)[:200]}")
        self._app_token = token
        self._app_token_expires_at = now + int(data.get("expires_in", 3600)) - 60
        logger.info("DA: minted client-credentials token (expires in %ss)",
                    data.get("expires_in", 0))
        return token

    async def _api_get(self, path: str, params) -> dict | None:
        """GET an official-API endpoint with the app token. Retries once on 401
        (stale token) and once on 429 (rate limit). Returns parsed JSON or None."""
        token = await self._get_app_token()
        url = f"{_API}{path}"
        try:
            resp = await self._http.get(url, params=params,
                                        headers={"Authorization": f"Bearer {token}"})
            if resp.status_code == 401:
                # Token likely expired early — force a refresh and retry once.
                self._app_token = ""
                token = await self._get_app_token()
                resp = await self._http.get(url, params=params,
                                            headers={"Authorization": f"Bearer {token}"})
            if resp.status_code == 429:
                logger.warning("DA: rate limited (429) on %s, waiting 30s...", path)
                await asyncio.sleep(30)
                resp = await self._http.get(url, params=params,
                                            headers={"Authorization": f"Bearer {token}"})
            resp.raise_for_status()
            return resp.json()
        except httpx.HTTPError as e:
            logger.error("DA: API GET %s failed: %s", path, e)
            return None
        except Exception as e:
            logger.error("DA: API GET %s parse error: %s", path, e)
            return None

    async def get_deviation_comments(self, deviationid: str,
                                     deviation_url: str = "") -> list[dict]:
        """Comments on a deviation via the official API (gap G3 inbox capture).

        ``GET /comments/deviation/{deviationid}`` with the same app token the
        stats polling uses (client-credentials; mature deviations may need a
        user token — degrade to empty rather than erroring). Returns one dict
        per comment: {comment_id, author, body, commented_at, permalink}.
        """
        data = await self._api_get(f"/comments/deviation/{deviationid}",
                                   {"maxdepth": 5, "limit": 50})
        if not isinstance(data, dict):
            return []
        out = []
        for c in data.get("thread") or []:
            cid = c.get("commentid")
            if not cid:
                continue
            user = c.get("user") or {}
            out.append({
                "comment_id": str(cid),
                "author": user.get("username", ""),
                "body": c.get("body", ""),
                "commented_at": c.get("posted", ""),
                # DA has no stable per-comment anchor — land on the deviation.
                "permalink": deviation_url or "",
            })
        return out

    async def whoami(self, access_token: str) -> dict | None:
        """Which DeviantArt account does this USER token authorise?

        The app token (client-credentials) says nothing about a user — it is
        the per-user token minted from ``da_refresh_token`` that decides which
        account a post lands on, and nothing checked it. Measured on the live
        install: the default DA account's stored refresh token authorised a
        DIFFERENT account, and one piece posted "as" the first landed on the
        second. That is the 3.21.0 bare-key incident's residue — the cause was
        fixed, the credential never repaired.

        ⚠ **Header auth only.** DA rejects ``?access_token=`` on this endpoint
        with ``401 invalid_token: "Expired oAuth2 token"`` — a misleading error
        for what is actually the wrong transport, and one that would send an
        investigation straight back to token expiry. Measured: the same token
        returns 200 through ``Authorization: Bearer`` and 401 as a query
        parameter, in the same second.

        Returns ``{"username", "userid", "type"}`` or ``None``.
        """
        if not access_token:
            return None
        try:
            resp = await self._http.get(
                f"{_API}/user/whoami",
                headers={"Authorization": f"Bearer {access_token}"},
                timeout=20.0)
            if resp.status_code != 200:
                logger.warning("DA whoami: HTTP %s — %s",
                               resp.status_code, resp.text[:160])
                return None
            data = resp.json()
            return data if data.get("username") else None
        except Exception as e:
            logger.warning("DA whoami failed: %s", e)
            return None

    async def validate_credentials(self) -> bool:
        """Validate the credentials **polling** uses.

        OAuth path: mint an app token and confirm ``/gallery/all`` responds for
        the target user (an empty gallery still counts as valid). Cookie path:
        defer to :meth:`validate_cookies`.

        ⚠ **This says nothing about posting.** The app token is
        client-credentials — it is not tied to any user, and ``/gallery/all`` is
        a public listing, so a pass here means "this app works and that username
        exists", not "we can post as them". Posting authenticates with the
        per-user token minted from ``da_refresh_token``; check that with
        :meth:`whoami` (see ``DeviantArtPoster.validate_session``). Conflating
        the two is how an account reported healthy while its posting token
        belonged to somebody else.
        """
        if self._use_oauth:
            if not self.target_user:
                logger.warning("DA: no target_user set for OAuth validation")
                return False
            try:
                await self._get_app_token()
            except Exception as e:
                logger.warning("DA: token mint failed during validation: %s", e)
                return False
            data = await self._api_get(
                "/gallery/all",
                {"username": self.target_user, "limit": 1, "mature_content": "true"},
            )
            if data is None:
                return False
            if data.get("error"):
                logger.warning("DA: gallery/all validation error: %s — %s",
                               data.get("error"), data.get("error_description"))
                return False
            return "results" in data
        return await self.validate_cookies()

    async def get_follower_count(self) -> int | None:
        """Best-effort watcher (follower) count for the target user.

        Uses the official /user/profile/{username} endpoint. The app-only
        client-credentials token may lack the `user` scope for this call on some
        apps; on any failure it returns None so DA simply shows no follower data
        rather than erroring the poll. Cookie-only installs skip it.
        """
        if not self._use_oauth or not self.target_user:
            return None
        data = await self._api_get(f"/user/profile/{self.target_user}", None)
        if not data or not isinstance(data, dict) or data.get("error"):
            return None
        stats = data.get("stats", {}) or {}
        watchers = stats.get("watchers")
        if watchers is None:
            watchers = data.get("watchers")
        if watchers is None:
            return None
        try:
            return int(watchers)
        except (TypeError, ValueError):
            return None

    async def uuid_for(self, deviation_id: str | int) -> str:
        """Our integer deviation id → the API's UUID.

        DeviantArt has **two** identifiers for one deviation and they are not
        interchangeable: the integer at the end of every public URL, and the
        GUID the API calls ``deviationid``. This module's contract (see the
        header) is that the integer is what gets stored — it is what the URLs
        carry, what the poller writes into ``da_submissions``, and therefore
        what every link, hash and publication row is keyed on. The UUID is a
        call-time detail.

        But the write endpoints (``/deviation/literature/update/{id}``) want the
        UUID, so anything editing a deviation has to convert. That is this.
        Warms the gallery cache once if it is cold; a UUID handed in is returned
        unchanged, so callers holding a legacy row (or a UUID straight from a
        create response) still work.
        """
        sid = str(deviation_id or "").strip()
        if not sid:
            return ""
        if not sid.isdigit():
            return sid          # already a UUID (or a legacy row) — pass through
        did = int(sid)
        cached = self._gallery_cache.get(did)
        if not cached or not cached.get("uuid"):
            # Cold cache: one gallery enumeration fills it for every deviation.
            await self.get_all_deviation_ids()
            cached = self._gallery_cache.get(did)
        return (cached or {}).get("uuid", "")

    async def get_all_deviation_ids(self) -> list[dict]:
        """Enumerate the target user's deviations (OAuth primary, cookie fallback)."""
        if self._use_oauth:
            return await self._get_all_deviation_ids_oauth()
        return await self._get_all_deviation_ids_cookie()

    async def get_deviation_details_batch(self, deviation_ids: list[int]) -> list[dict]:
        """Fetch stats/metadata for many deviations (OAuth primary, cookie fallback)."""
        if self._use_oauth:
            return await self._get_details_batch_oauth(deviation_ids)
        return await self._get_details_batch_cookie(deviation_ids)

    async def _get_all_deviation_ids_oauth(self) -> list[dict]:
        """[Official API] Enumerate the target user's gallery via /gallery/all.

        Caches per-deviation fields (UUID, title, url, thumbnail, date, mature)
        keyed by the integer deviation id parsed from the URL, so the details
        step can reuse them. Returns ``[{deviation_id: int, title: str}, ...]``.
        """
        self._gallery_cache.clear()
        out: list[dict] = []
        seen: set[int] = set()
        offset = 0

        for _page_safety in range(1000):
            data = await self._api_get("/gallery/all", {
                "username": self.target_user,
                "offset": offset,
                "limit": 24,
                "mature_content": "true",  # include mature deviations
            })
            if data is None:
                logger.error("DA: gallery/all returned no data at offset %d", offset)
                break
            if data.get("error"):
                logger.error("DA: gallery/all error: %s — %s",
                             data.get("error"), data.get("error_description"))
                break

            results = data.get("results") or []
            if not results:
                break

            new_this_page = 0
            for d in results:
                did = _int_id_from_url(d.get("url", ""))
                if did is None or did in seen:
                    continue
                seen.add(did)
                self._gallery_cache[did] = {
                    "uuid": d.get("deviationid", ""),
                    "title": d.get("title", ""),
                    "url": d.get("url", ""),
                    "thumbnail_url": _pick_thumb(d),
                    "posted_at": _unix_to_iso(d.get("published_time")),
                    "is_mature": bool(d.get("is_mature")),
                    "username": (d.get("author") or {}).get("username", self.target_user),
                }
                out.append({"deviation_id": did, "title": d.get("title", "")})
                new_this_page += 1

            if new_this_page == 0 or not data.get("has_more"):
                break
            next_offset = data.get("next_offset")
            offset = next_offset if next_offset is not None else offset + 24
            await asyncio.sleep(config.DA_REQUEST_DELAY_SECONDS)

        logger.info("DA: gallery/all found %d deviations for %s", len(out), self.target_user)
        return out

    async def _get_details_batch_oauth(self, deviation_ids: list[int]) -> list[dict]:
        """[Official API] Fetch stats for many deviations via metadata?ext_stats.

        Chunks by 10 (the ext_stats cap), maps each int id to its cached UUID,
        and merges the metadata stats with the gallery-cache fields into the same
        detail-dict shape the legacy path produced.
        """
        details: list[dict] = []
        for chunk in _chunks(deviation_ids, _META_BATCH):
            int_by_uuid: dict[str, int] = {}
            params: list[tuple[str, str]] = [("ext_stats", "true"),
                                             ("ext_submission", "true"),
                                             ("mature_content", "true")]
            for did in chunk:
                cached = self._gallery_cache.get(did)
                if not cached or not cached.get("uuid"):
                    logger.debug("DA: no cached UUID for deviation %s — skipping", did)
                    continue
                uuid = cached["uuid"]
                int_by_uuid[uuid.upper()] = did
                params.append(("deviationids[]", uuid))
            if not int_by_uuid:
                continue

            data = await self._api_get("/deviation/metadata", params)
            if not data or "metadata" not in data:
                logger.warning("DA: metadata returned no data for a chunk of %d", len(int_by_uuid))
                continue
            for m in data.get("metadata", []):
                uuid = (m.get("deviationid") or "").upper()
                did = int_by_uuid.get(uuid)
                if did is None:
                    continue
                details.append(self._build_detail(did, m))
            await asyncio.sleep(config.DA_REQUEST_DELAY_SECONDS)
        return details

    def _build_detail(self, deviation_id: int, m: dict) -> dict:
        """Merge a metadata object + gallery cache into a legacy-shaped detail dict."""
        cached = self._gallery_cache.get(deviation_id, {})
        stats = m.get("stats") or {}
        tags = [t.get("tag_name", "") for t in (m.get("tags") or []) if isinstance(t, dict)]
        subm = m.get("submission") or {}
        return {
            "deviation_id": deviation_id,
            # Official-API UUID — the comments endpoint (gap G3) is keyed by it.
            "uuid": cached.get("uuid", ""),
            "title": m.get("title") or cached.get("title", ""),
            "username": (m.get("author") or {}).get("username")
                        or cached.get("username", self.target_user),
            "description": _strip_html(m.get("description", "")),
            "category": subm.get("category", "") or "",
            "rating": "Mature" if m.get("is_mature") else "General",
            "views": stats.get("views", 0) or 0,
            "favorites_count": stats.get("favourites", 0) or 0,
            "comments_count": stats.get("comments", 0) or 0,
            "downloads": stats.get("downloads", 0) or 0,
            "keywords": tags,
            "link": cached.get("url", "") or f"{_BASE}/deviation/{deviation_id}",
            "thumbnail_url": cached.get("thumbnail_url", ""),
            "posted_at": cached.get("posted_at", ""),
        }

    # ── Legacy cookie / Eclipse _napi path (fallback) ─────────

    async def validate_cookies(self) -> bool:
        """Test cookies against a page only a signed-in session can load.

        ⚠ The previous version fetched ``/{target_user}/gallery`` — a **public**
        page — and returned true on ``data-userid`` or a notifications link.
        A deviant's gallery carries ``data-userid`` for its owner whether or not
        the viewer is logged in, so this was the FurAffinity ``<figure>`` bug
        (3.19.1) a second time: a check structurally incapable of failing,
        sitting in the one place expiry was supposed to be reported.

        This path is a fallback — ``_use_oauth`` wins whenever app credentials
        exist — which is exactly why it was never caught. Fixed rather than
        deleted because an install without app credentials still reaches it, and
        a check that cannot fail is worse than no check: it answers.

        Asks for ``/settings/`` instead, which redirects a signed-out visitor to
        the login page. Fails CLOSED — anything unexpected is invalid, because a
        false negative costs a re-paste while a false positive costs a silent
        misattributed failure.
        """
        if not self.cookie_value or not self.target_user:
            return False
        try:
            html = await self._get_page(f"{_BASE}/settings/")
            if not html:
                return False
            low = html.lower()
            if "/users/logout" in low or 'data-hook="user_menu"' in low:
                return True
            logger.info("DA cookie validation: no signed-in marker on /settings/ "
                        "— the session is not authenticated")
            return False
        except Exception as e:
            logger.warning("DA: Cookie validation failed: %s", e)
            return False

    # ── Gallery Discovery ─────────────────────────────────────

    async def _get_all_deviation_ids_cookie(self) -> list[dict]:
        """[Legacy cookie/_napi] Fetch all deviation IDs for the target user."""
        all_deviations: list[dict] = []
        offset = 0
        limit = 24
        seen_ids: set[int] = set()

        for _page_safety in range(1000):
            url = f"{_BASE}/_napi/da-user-profile/api/gallery/contents"
            params = {
                "username": self.target_user,
                "offset": str(offset),
                "limit": str(limit),
                "all_folder": "true",
                "mode": "newest",
            }

            logger.info("DA: Fetching gallery page offset=%d for %s", offset, self.target_user)
            data = await self._get_json(url, params=params)

            if not data:
                # Fallback: try scraping the gallery page HTML
                logger.info("DA: _napi failed, trying HTML scrape fallback")
                return await self._scrape_gallery_ids()

            results = data.get("results", [])
            if not results:
                break

            new_this_page = 0
            for item in results:
                deviation = item.get("deviation", item)
                dev_id = deviation.get("deviationId")
                if dev_id and dev_id not in seen_ids:
                    seen_ids.add(dev_id)
                    all_deviations.append({
                        "deviation_id": dev_id,
                        "title": deviation.get("title", ""),
                    })
                    new_this_page += 1

            if new_this_page == 0:
                break

            has_more = data.get("hasMore", False)
            next_offset = data.get("nextOffset")

            if not has_more:
                break

            offset = next_offset if next_offset is not None else offset + limit
            await asyncio.sleep(config.DA_REQUEST_DELAY_SECONDS)

        logger.info("DA: Found %d deviations for %s", len(all_deviations), self.target_user)
        return all_deviations

    async def _scrape_gallery_ids(self) -> list[dict]:
        """Fallback: scrape deviation IDs from gallery HTML pages."""
        all_deviations: list[dict] = []
        seen_ids: set[int] = set()
        page = 0

        for _page_safety in range(1000):
            url = f"{_BASE}/{self.target_user}/gallery?offset={page * 24}"
            html = await self._get_page(url)
            if not html:
                break

            # Find deviation links in gallery HTML
            matches = re.findall(
                r'data-deviationid="(\d+)"[^>]*>.*?class="[^"]*title[^"]*"[^>]*>([^<]*)<',
                html, re.DOTALL,
            )
            if not matches:
                # Alternative pattern
                matches = re.findall(
                    r'/art/[^"]*-(\d+)"[^>]*title="([^"]*)"',
                    html,
                )

            if not matches:
                break

            new_this_page = 0
            for dev_id_str, title in matches:
                dev_id = int(dev_id_str)
                if dev_id not in seen_ids:
                    seen_ids.add(dev_id)
                    all_deviations.append({
                        "deviation_id": dev_id,
                        "title": unescape(title.strip()),
                    })
                    new_this_page += 1

            if new_this_page == 0:
                break

            page += 1
            await asyncio.sleep(config.DA_REQUEST_DELAY_SECONDS)

        logger.info("DA: Scraped %d deviations for %s", len(all_deviations), self.target_user)
        return all_deviations

    # ── Deviation Details ─────────────────────────────────────

    async def get_deviation_detail(self, deviation_id: int) -> dict:
        """Fetch stats and metadata for a single deviation."""
        detail: dict = {"deviation_id": deviation_id}

        # Try the _napi extended fetch endpoint
        url = f"{_BASE}/_napi/shared_api/deviation/extended_fetch"
        params = {
            "deviationid": str(deviation_id),
            "username": self.target_user,
            "type": "art",
            "include_session": "false",
        }
        data = await self._get_json(url, params=params)

        if data and "deviation" in data:
            dev = data["deviation"]
            detail["title"] = dev.get("title", "")
            detail["username"] = dev.get("author", {}).get("username", self.target_user)
            detail["description"] = dev.get("textContent", {}).get("excerpt", "")

            # Category from media info
            detail["category"] = dev.get("categoryPath", "")
            detail["rating"] = "Mature" if dev.get("isMature") else "General"

            # Stats
            stats = dev.get("stats", {})
            detail["views"] = stats.get("views", 0)
            detail["favorites_count"] = stats.get("favourites", 0)
            detail["comments_count"] = stats.get("comments", 0)
            detail["downloads"] = stats.get("downloads", 0)

            # Tags/keywords
            tags = data.get("tags", [])
            detail["keywords"] = [t.get("name", "") for t in tags if isinstance(t, dict)]

            # URL and thumbnail
            detail["link"] = dev.get("url", f"{_BASE}/art/-{deviation_id}")
            media = dev.get("media", {})
            if media and "baseUri" in media:
                detail["thumbnail_url"] = media["baseUri"]
            else:
                detail["thumbnail_url"] = ""

            # Dates
            detail["posted_at"] = dev.get("publishedTime", "")

        else:
            # Fallback: scrape the deviation page HTML
            detail = await self._scrape_deviation_detail(deviation_id)

        return detail

    async def _scrape_deviation_detail(self, deviation_id: int) -> dict:
        """Fallback: scrape deviation details from the HTML page."""
        detail: dict = {"deviation_id": deviation_id}

        # Try to find the deviation URL
        url = f"{_BASE}/art/-{deviation_id}"
        html = await self._get_page(url)

        if not html:
            detail.update({
                "title": "", "username": self.target_user, "views": 0,
                "favorites_count": 0, "comments_count": 0, "downloads": 0,
                "keywords": [], "link": url, "description": "",
                "category": "", "rating": "", "thumbnail_url": "",
                "posted_at": "",
            })
            return detail

        # Title
        m = re.search(r'<title>([^<]+)</title>', html)
        if m:
            title = m.group(1).split(" by ")[0].strip()
            detail["title"] = unescape(title)
        else:
            detail["title"] = ""

        detail["username"] = self.target_user
        detail["link"] = url
        detail["description"] = ""
        detail["category"] = ""
        detail["rating"] = ""
        detail["thumbnail_url"] = ""
        detail["posted_at"] = ""
        detail["keywords"] = []

        # Try to extract stats from page JSON data
        stats_match = re.search(r'"stats"\s*:\s*\{([^}]+)\}', html)
        if stats_match:
            stats_text = stats_match.group(1)
            detail["views"] = _extract_stat_int(stats_text, "views")
            detail["favorites_count"] = _extract_stat_int(stats_text, "favourites")
            detail["comments_count"] = _extract_stat_int(stats_text, "comments")
            detail["downloads"] = _extract_stat_int(stats_text, "downloads")
        else:
            detail["views"] = 0
            detail["favorites_count"] = 0
            detail["comments_count"] = 0
            detail["downloads"] = 0

        return detail

    async def _get_details_batch_cookie(self, deviation_ids: list[int]) -> list[dict]:
        """[Legacy cookie/_napi] Fetch details for multiple deviations sequentially."""
        details = []
        for i, dev_id in enumerate(deviation_ids):
            if i > 0:
                await asyncio.sleep(config.DA_REQUEST_DELAY_SECONDS)
            try:
                detail = await self.get_deviation_detail(dev_id)
                details.append(detail)
            except Exception as e:
                logger.warning("DA: Failed to fetch deviation %d: %s", dev_id, e)
        return details


    # ── OAuth2 Posting (Official API) ─────────────────────────────

    async def oauth_create_literature(
        self,
        *,
        title: str,
        body: str,
        tags: list[str] | None = None,
        is_mature: bool = False,
        mature_level: str = "",
        mature_classification: list[str] | None = None,
        allow_comments: bool = True,
        galleryids: list[str] | None = None,
        access_token: str = "",
    ) -> dict:
        """Create a literature deviation via the official OAuth2 API.

        Requires a valid OAuth2 access token with 'user.manage' scope.
        Register an app at the DeviantArt developer portal to get
        client_id/client_secret, then do the Authorization Code flow.

        Args:
            title: Deviation title (max 50 chars).
            body: Literature text content (plain text or limited HTML).
            tags: List of tags.
            is_mature: Whether content is mature/adult.
            mature_level: "strict" or "moderate" (required if is_mature).
            mature_classification: List of: "nudity", "sexual", "gore", "language", "ideology".
            allow_comments: Allow comments on the deviation.
            galleryids: Gallery folder UUIDs to add to.
            access_token: OAuth2 access token.

        Returns:
            Dict with 'deviationid'.
        """
        if not access_token:
            raise RuntimeError("DA: OAuth2 access token required")

        params = {
            "title": title[:50],
            "body": body,
            "allow_comments": "1" if allow_comments else "0",
            "access_token": access_token,
        }
        if tags:
            for i, tag in enumerate(tags[:30]):
                params[f"tags[{i}]"] = tag
        # Always sent — DA treats is_mature as required, not as a flag that
        # defaults to false. Same fault as stash/publish had; kept in step here
        # deliberately rather than waiting for literature to hit it too.
        params["is_mature"] = "1" if is_mature else "0"
        if is_mature:
            if mature_level:
                params["mature_level"] = mature_level
            # Indexed. The `[]` form assigned the same key each pass, so only
            # the last classification survived the loop.
            for i, mc in enumerate(mature_classification or []):
                params[f"mature_classification[{i}]"] = mc
        if galleryids:
            for i, gid in enumerate(galleryids):
                params[f"galleryids[{i}]"] = gid

        resp = await self._http.post(
            "https://www.deviantart.com/api/v1/oauth2/deviation/literature/create",
            data=params,
            timeout=60.0,
        )

        if resp.status_code == 401:
            raise RuntimeError("DA: OAuth token expired or invalid (401)")
        if resp.status_code != 200:
            raise RuntimeError(f"DA: Literature create failed — {resp.status_code}: {resp.text[:200]}")

        result = resp.json()
        dev_id = result.get("deviationid", "")
        logger.info("DA: Created literature deviation %s — %s", dev_id, title[:40])
        # Build the page URL from the configured account, not a hardcoded one.
        return {"deviationid": dev_id,
                "url": f"https://www.deviantart.com/{self.target_user}/art/{dev_id}"}

    async def oauth_stash_submit(
        self,
        file_path: str,
        *,
        title: str = "",
        artist_comments: str = "",
        tags: list[str] | None = None,
        access_token: str = "",
    ) -> dict:
        """Upload an image to Sta.sh (POST /api/v1/oauth2/stash/submit).

        Step 1 of image publishing — stashes the file and returns its itemid.
        The DA app's access token must include the **stash** scope; an app set
        up for literature-only (user.manage) will 401/403 until re-authorized.

        Returns a dict with 'itemid' (+ 'stackid' if present).
        """
        if not access_token:
            raise RuntimeError("DA: OAuth2 access token required")
        import os
        with open(file_path, "rb") as f:
            file_data = f.read()
        filename = os.path.basename(file_path)
        ext = os.path.splitext(filename)[1].lstrip(".").lower()
        mime = {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg",
                "gif": "image/gif", "webp": "image/webp"}.get(ext, "application/octet-stream")

        data = {"title": title[:50], "access_token": access_token}
        if artist_comments:
            data["artist_comments"] = artist_comments
        # DA's schema types `tags` as an ARRAY of strings. Until 3.17.2 this
        # sent `", ".join(tags)` — one string containing commas and spaces,
        # which also violates DA's "letters, numbers and underscore only" rule,
        # so the whole set was discarded and deviations published untagged.
        # Indexed keys, matching `galleryids[i]` in oauth_stash_publish below;
        # the bare `tags[]` form silently keeps only the last value in a dict.
        clean, dropped = sanitize_tags(tags)
        for i, tag in enumerate(clean[:30]):
            data[f"tags[{i}]"] = tag
        if dropped:
            logger.info("DA: %d tag(s) dropped as unusable: %s",
                        len(dropped), ", ".join(dropped[:5]))

        resp = await self._http.post(
            "https://www.deviantart.com/api/v1/oauth2/stash/submit",
            data=data,
            files={"file": (filename, file_data, mime)},
            timeout=120.0,
        )
        if resp.status_code == 401:
            raise RuntimeError("DA: OAuth token expired or invalid (401)")
        if resp.status_code != 200:
            raise RuntimeError(f"DA: stash/submit failed — {resp.status_code}: {resp.text[:200]}")
        result = resp.json()
        itemid = result.get("itemid")
        if not itemid:
            raise RuntimeError(f"DA: stash/submit response missing itemid: {str(result)[:200]}")
        logger.info("DA: stashed image itemid=%s", itemid)
        return {"itemid": itemid, "stackid": result.get("stackid")}

    async def oauth_stash_publish(
        self,
        itemid: str | int,
        *,
        is_mature: bool = False,
        mature_level: str = "",
        mature_classification: list[str] | None = None,
        catpath: str = "",
        galleryids: list[str] | None = None,
        allow_comments: bool = True,
        tags: list[str] | None = None,
        noai: bool = True,
        access_token: str = "",
    ) -> dict:
        """Publish a stashed item to the gallery (POST /stash/publish).

        Step 2 of image publishing. Requires the **publish** scope. DA mandates
        agreeing to the submission policy + ToS, so agree_submission/agree_tos
        are always sent as "1". Returns a dict with 'deviationid' and 'url'.
        """
        if not access_token:
            raise RuntimeError("DA: OAuth2 access token required")
        params: dict[str, str] = {
            "itemid": str(itemid),
            "agree_submission": "1",
            "agree_tos": "1",
            "allow_comments": "1" if allow_comments else "0",
            "access_token": access_token,
        }
        # ALWAYS sent, both ways. DeviantArt treats is_mature as required, not
        # as a flag that defaults to false when absent:
        #
        #   400 invalid_request — {"is_mature": "is_mature is required"}
        #
        # Omitting it on a general-rated piece therefore blocked every SFW
        # image post, and only those — which is why this survived: the mature
        # path filled the field in and worked.
        params["is_mature"] = "1" if is_mature else "0"
        if is_mature:
            if mature_level:
                params["mature_level"] = mature_level
            # Indexed, matching galleryids below. The previous form assigned
            # `mature_classification[]` inside the loop, so each value
            # overwrote the last and only ONE ever reached DA — a piece marked
            # as several things arrived marked as whichever came last.
            for i, mc in enumerate(mature_classification or []):
                params[f"mature_classification[{i}]"] = mc
        if catpath:
            params["catpath"] = catpath
        if galleryids:
            for i, gid in enumerate(galleryids):
                params[f"galleryids[{i}]"] = gid
        # `tags` is a DEVIATION-level field on publish, not just stash metadata.
        # Sending it here as well as on submit is deliberate: publish is what
        # creates the deviation, and the stash item's copy is the one that has
        # to survive the hand-off. One computation, two sinks — the caller
        # passes the same list to both.
        clean, _dropped = sanitize_tags(tags)
        for i, tag in enumerate(clean[:30]):
            params[f"tags[{i}]"] = tag
        # DA exposes an explicit "do not use this work to train AI" flag. This
        # project's whole positioning is that the artists it serves object to
        # generative AI, so it is set by DEFAULT rather than offered as an
        # option nobody would find. `is_ai_generated` is deliberately never
        # sent — nothing here is generated.
        params["noai"] = "1" if noai else "0"

        resp = await self._http.post(
            "https://www.deviantart.com/api/v1/oauth2/stash/publish",
            data=params,
            timeout=60.0,
        )
        if resp.status_code == 401:
            raise RuntimeError("DA: OAuth token expired or invalid (401)")
        if resp.status_code != 200:
            raise RuntimeError(f"DA: stash/publish failed — {resp.status_code}: {resp.text[:200]}")
        result = resp.json()
        dev_id = result.get("deviationid", "")
        url = result.get("url", "") or f"https://www.deviantart.com/deviation/{dev_id}"
        logger.info("DA: published deviation %s from stash", dev_id)
        return {"deviationid": dev_id, "url": url}

    async def oauth_update_literature(
        self,
        deviation_id: str,
        *,
        title: str | None = None,
        body: str | None = None,
        tags: list[str] | None = None,
        is_mature: bool | None = None,
        access_token: str = "",
    ) -> dict:
        """Update an existing literature deviation via OAuth2 API.

        Only provided fields are updated.
        """
        if not access_token:
            raise RuntimeError("DA: OAuth2 access token required")

        params: dict[str, str] = {
            "access_token": access_token,
        }
        if title is not None:
            params["title"] = title[:50]
        if body is not None:
            params["body"] = body
        if tags is not None:
            for i, tag in enumerate(tags[:30]):
                params[f"tags[{i}]"] = tag
        if is_mature is not None:
            params["is_mature"] = "1" if is_mature else "0"

        resp = await self._http.post(
            f"https://www.deviantart.com/api/v1/oauth2/deviation/literature/update/{deviation_id}",
            data=params,
            timeout=30.0,
        )

        if resp.status_code == 401:
            raise RuntimeError("DA: OAuth token expired or invalid (401)")
        if resp.status_code != 200:
            raise RuntimeError(f"DA: Literature update failed — {resp.status_code}: {resp.text[:200]}")

        logger.info("DA: Updated literature deviation %s", deviation_id)
        return resp.json()

    async def oauth_refresh_token(
        self,
        client_id: str,
        client_secret: str,
        refresh_token: str,
    ) -> dict:
        """Refresh an OAuth2 access token.

        Access tokens expire after 1 hour. Refresh tokens last 3 months.
        Returns new access_token and refresh_token.
        """
        resp = await self._http.post(
            "https://www.deviantart.com/oauth2/token",
            data={
                "grant_type": "refresh_token",
                "client_id": client_id,
                "client_secret": client_secret,
                "refresh_token": refresh_token,
            },
            timeout=15.0,
        )

        if resp.status_code != 200:
            body = resp.text[:300]
            # A dead refresh token is a 400 with "The refresh_token is invalid."
            # No amount of retrying fixes it — it needs the operator to
            # re-authorise — so name it in words both the human and
            # manager._PERMANENT_ERROR_MARKERS can act on. Until 3.21.0 this
            # raised the bare status code, and three days of failures said only
            # "400" while the actual sentence sat unread in the response body.
            if "refresh_token is invalid" in body or "invalid_grant" in body:
                raise RuntimeError(
                    "DA: the stored refresh token is no longer valid — re-authorise "
                    "this account (Settings -> Accounts -> DeviantArt -> Connect). "
                    f"DeviantArt said: {body}"
                )
            raise RuntimeError(f"DA: Token refresh failed — {resp.status_code}: {body}")

        data = resp.json()
        logger.info("DA: OAuth token refreshed (expires in %ds)", data.get("expires_in", 0))
        return data


def _extract_stat_int(text: str, key: str) -> int:
    """Extract an integer stat value from a JSON-like string."""
    m = re.search(rf'"{key}"\s*:\s*(\d+)', text)
    return int(m.group(1)) if m else 0


# ── Official-API helpers ──────────────────────────────────────

_URL_ID_RE = re.compile(r"-(\d+)/?$")


def _int_id_from_url(url: str) -> int | None:
    """Parse the trailing integer deviation id from a deviation URL.

    e.g. ``https://www.deviantart.com/user/art/Some-Title-1351251437`` -> 1351251437.
    Returns None if no trailing id is present (e.g. status updates).
    """
    if not url:
        return None
    m = _URL_ID_RE.search(url)
    return int(m.group(1)) if m else None


def _pick_thumb(dev: dict) -> str:
    """Pick a thumbnail URL from a gallery/all deviation object.

    Prefers the largest ``thumbs`` entry, then ``content.src``, then ``preview.src``.
    """
    thumbs = dev.get("thumbs") or []
    if thumbs:
        # thumbs are ordered small→large; take the last (largest) with a src.
        for t in reversed(thumbs):
            if isinstance(t, dict) and t.get("src"):
                return t["src"]
    for key in ("content", "preview"):
        node = dev.get(key) or {}
        if isinstance(node, dict) and node.get("src"):
            return node["src"]
    return ""


def _unix_to_iso(ts) -> str:
    """Convert a Unix timestamp (int or digit-string) to 'YYYY-MM-DD HH:MM:SS' UTC.

    Passes through anything already non-numeric (or empty) as a best-effort string.
    """
    if ts is None or ts == "":
        return ""
    try:
        return datetime.fromtimestamp(int(ts), tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    except (ValueError, TypeError, OSError):
        return str(ts)


def _strip_html(s: str, limit: int = 2000) -> str:
    """Reduce an HTML description to plain text.

    Unescape entities FIRST, then strip tags — so escaped markup like
    ``&lt;script&gt;`` can't be un-escaped back into a live tag after the strip
    pass. Defence-in-depth: DA descriptions aren't rendered in the dashboard
    today, but the stored value stays tag-free regardless.
    """
    if not s:
        return ""
    text = unescape(s)                    # entities -> literal chars first
    text = re.sub(r"<[^>]+>", "", text)   # then strip any (now-literal) tags
    return text.strip()[:limit]


def _chunks(lst: list, n: int):
    """Yield successive n-sized chunks from *lst*."""
    for i in range(0, len(lst), n):
        yield lst[i:i + n]

    # ── Editing a published deviation ────────────────────────────────────────

    async def oauth_edit_deviation(
        self,
        deviation_id: str,
        *,
        title: str | None = None,
        tags: list[str] | None = None,
        is_mature: bool | None = None,
        mature_level: str = "moderate",
        mature_classification: list[str] | None = None,
        galleryids: list[str] | None = None,
        allow_comments: bool | None = None,
        access_token: str = "",
    ) -> dict:
        """Edit a published deviation's metadata (``POST /deviation/edit/{id}``).

        Works for IMAGE deviations, not only literature — which is the whole
        point, since ``deviation/literature/update`` does not. Requires the
        ``publish`` or ``stash`` scope.

        ⚠ **There is no description parameter on this endpoint** — the full body
        is title / is_mature / mature_level / mature_classification /
        allow_comments / display_resolution / galleryids / allow_free_download /
        add_watermark / license_options / tags / subject_tags / location_tag /
        groups / is_ai_generated / noai. Use :meth:`napi_set_description` for the
        description; the two together are a complete metadata sync.

        Only the fields passed are sent, so an unspecified field keeps its value.
        """
        if not access_token:
            raise RuntimeError("DA: OAuth2 access token required")

        params: dict[str, str] = {"access_token": access_token}
        if title is not None:
            params["title"] = title[:50]
        if tags is not None:
            for i, tag in enumerate(tags[:30]):
                params[f"tags[{i}]"] = tag
        if is_mature is not None:
            params["is_mature"] = "1" if is_mature else "0"
            if is_mature:
                # Mandatory whenever is_mature is true — DA 400s without it.
                params["mature_level"] = mature_level or "moderate"
                for i, c in enumerate(mature_classification or []):
                    params[f"mature_classification[{i}]"] = c
        if galleryids is not None:
            for i, g in enumerate(galleryids):
                params[f"galleryids[{i}]"] = g
        if allow_comments is not None:
            params["allow_comments"] = "1" if allow_comments else "0"

        resp = await self._http.post(
            f"https://www.deviantart.com/api/v1/oauth2/deviation/edit/{deviation_id}",
            data=params, timeout=60.0,
        )
        if resp.status_code != 200:
            raise RuntimeError(
                f"DA: edit failed ({resp.status_code}): {resp.text[:200]}")
        return resp.json() if resp.content else {}

    async def _eclipse_csrf_token(self) -> str:
        """Scrape a CSRF token for the Eclipse (``_napi``) endpoints.

        The description endpoint is a website endpoint, not an API one: it wants
        the session cookie plus a matching CSRF token. The token is embedded in
        any logged-in page's bootstrap JSON.
        """
        resp = await self._http.get("https://www.deviantart.com/", timeout=30.0)
        m = re.search(r'"csrfToken"\s*:\s*"([^"]+)"', resp.text) or \
            re.search(r'csrf_token["\'\s:=]+([A-Za-z0-9_.\-]{20,})', resp.text)
        if not m:
            raise RuntimeError(
                "DA: could not find a CSRF token — the da_cookie is probably "
                "expired or not logged in")
        return m.group(1)

    async def napi_set_description(self, deviation_id: str, text: str,
                                   csrf_token: str = "") -> dict:
        """Set a published deviation's DESCRIPTION.

        ``POST /_napi/shared_api/deviation/update`` — the call DA's own editor
        makes when you press Update. Captured from the live editor because the
        official API exposes no description field at all.

        ⚠ **Session auth, not OAuth**: this needs the ``da_cookie`` and a CSRF
        token, and therefore the CF Worker proxy from a datacenter IP (DA blocks
        those on the frontend, though not on the API). That is a deliberate step
        back towards the cookie the official-API migration was shedding — it buys
        the one field OAuth cannot reach.
        """
        if not self.cookie_value:
            raise RuntimeError(
                "DA: setting a description needs the da_cookie (the official API "
                "has no description field)")
        token = csrf_token or await self._eclipse_csrf_token()
        payload = {
            "deviationid": str(deviation_id),
            "editorRaw": description_to_editor_raw(text),
            "da_minor_version": 20230710,
            "csrf_token": token,
        }
        resp = await self._http.post(
            "https://www.deviantart.com/_napi/shared_api/deviation/update",
            json=payload, timeout=60.0,
        )
        if resp.status_code != 200:
            raise RuntimeError(
                f"DA: description update failed ({resp.status_code}): {resp.text[:200]}")
        logger.info("DA: set description on %s (%d chars)", deviation_id, len(text or ""))
        return resp.json() if resp.content else {}
