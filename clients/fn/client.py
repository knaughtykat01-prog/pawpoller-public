"""FurryNetwork API client — polling (own submissions + stats) and posting.

FurryNetwork (furrynetwork.com) organises a user's work under one or more
**characters** (a persona layer). Auth is OAuth2 password grant against
``https://furrynetwork.com/api`` with the public web ``client_id=123``; the
access token lasts ~1h and is renewed from the stored refresh token.

References: CrosspostSharp's `FurryNetworkClient.cs` (posting flow, endpoints)
and JustAnOpossum/FurryNetworkAPI (OAuth). PostyBirb dropped FN in its rewrite,
so those are the best available references — several response shapes here are
built to the documented model and should be **verified live** against a real
account (the Threads/IG pattern). Confirmed 2026-07-31: the API host is up and
reachable from the GCP VM (no datacenter block, unlike FurAffinity).

⚠ **The password grant is dead as of 2026-08-19.** ``POST /api/oauth/token`` with
``grant_type=password`` now answers **422 ``{"message": "Invalid Recaptcha
Token"}``** before it ever looks at the credentials — measured directly, with
and without ``client_id``, and with an empty ``recaptcha`` field added. FN put
its login behind reCAPTCHA, which no headless client can satisfy, so no email
and password will ever authenticate this app again.

**The refresh grant is untouched.** ``grant_type=refresh_token`` with a bogus
token still returns a normal ``400 {"error": "invalid_grant"}`` — proof that
reCAPTCHA is enforced on the password path only. The rest of the API is
likewise fine (an unauthenticated search returns 200). So the working shape is:

1. log in to furrynetwork.com in a browser, where the real page solves the
   reCAPTCHA;
2. take the ``refresh_token`` out of the session (DevTools → Application →
   Local Storage, or the ``/api/oauth/token`` response in the Network tab);
3. save it as ``fn_refresh_token``.

``login()`` already prefers a refresh token over the password, and every
renewal writes the (possibly rotated) token back to settings, so this only has
to be done once unless FN invalidates the token. The stored password is now
dead weight but is kept, because a grant behind reCAPTCHA today may not be
tomorrow and re-entering it costs nothing.

The client mirrors the same contract the pollers/posters expect elsewhere:
``validate_session`` → username, ``get_all_post_uris`` → discovery list,
``get_post_details_batch`` → normalised submission dicts, ``upload_artwork``.
"""
from __future__ import annotations

import logging
import os
import re
import time
from typing import Any

import httpx

logger = logging.getLogger(__name__)

API_BASE = "https://furrynetwork.com/api"
SITE_BASE = "https://furrynetwork.com"
CLIENT_ID = "123"                 # FN's public web client
HTTP_TIMEOUT = 30.0
UPLOAD_TIMEOUT = 120.0
UPLOAD_CHUNK = 512 * 1024         # 512 KB — FN's resumable chunk size

# FN rating is an int 0..2. Map to our internal rating vocabulary and back.
_RATING_FROM_FN = {0: "general", 1: "mature", 2: "adult"}
_RATING_TO_FN = {"general": 0, "mature": 1, "adult": 2, "explicit": 2, "extreme": 2}


def _safe_int(v: Any) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return 0


# FurryNetwork tag rules, measured against its own validator on 2026-08-19 by
# PATCHing a probe set at a draft and reading the per-index verdicts back:
#
#     'abc' OK   'white_tiger' OK   'heart-shape' OK   'Upper_Case' OK   '123' OK
#     'ab' TOO_SHORT            'a' TOO_SHORT              '12' TOO_SHORT
#     'kii_(secondfur)' INVALID   'two words' INVALID   'tag.dot' INVALID
#     'tag+plus' INVALID   "tag'apos" INVALID   'tag&amp' INVALID
#     'tag:colon' INVALID  'tag/slash' INVALID  'tag!bang' INVALID
#     '<3' INVALID,TOO_SHORT
#
# So: letters, digits, underscore and hyphen only; three characters minimum;
# case is preserved and allowed. FN returns these as a per-tag array —
# ``{"errors":{"tags":[null,...,["INVALID"],...]}}`` — and rejects the WHOLE
# PATCH if any single tag fails, which is why two bad tags out of twenty-five
# cost the entire post.
_FN_TAG_BAD = re.compile(r"[^A-Za-z0-9_-]+")
_FN_TAG_MIN = 3


def sanitize_tags(tags: list[str] | None) -> tuple[list[str], list[str]]:
    """Coerce tags into what FurryNetwork accepts. Returns (kept, dropped).

    The catalogue is tagged booru-style, where ``kii_(secondfur)`` and ``<3``
    are ordinary tags — FA, Inkbunny and e621 all take them. FN does not, and it
    fails the whole submission rather than skipping the offender, so shipping
    the canonical set verbatim meant one parenthetical artist tag could sink a
    post that was otherwise perfect.

    Illegal characters become underscores rather than being deleted, so
    ``kii_(secondfur)`` survives as ``kii_secondfur`` (verified accepted)
    instead of collapsing into the meaningless ``kiisecondfur``. Anything
    still under the length floor after that is dropped — ``<3`` reduces to
    ``3``, which FN would reject anyway and which carries no meaning alone.
    """
    kept: list[str] = []
    dropped: list[str] = []
    seen: set[str] = set()
    for raw in tags or []:
        clean = _FN_TAG_BAD.sub("_", str(raw)).strip("_-")
        clean = re.sub(r"_{2,}", "_", clean)
        if len(clean) < _FN_TAG_MIN:
            dropped.append(str(raw))
            continue
        if clean.lower() in seen:
            continue
        seen.add(clean.lower())
        kept.append(clean)
    return kept, dropped


class FnAuthError(Exception):
    """Raised on a genuine auth failure (bad credentials / revoked token) so the
    session-check can distinguish it from a transient network blip."""


class FnRecaptchaError(FnAuthError):
    """FurryNetwork demanded a reCAPTCHA token for the password grant.

    A subclass so every existing ``except FnAuthError`` still catches it, but
    callers that want to say something more useful than "auth failed" can. This
    is not a credentials problem and retrying will never clear it — see the
    module docstring.
    """


class FnClient:
    def __init__(self, username: str = "", password: str = "",
                 access_token: str = "", refresh_token: str = ""):
        # `username` here is the FN login email; the display name comes from /user.
        self.username = username
        self.password = password
        self.access_token = access_token
        self.refresh_token = refresh_token
        self._token_expiry = 0.0
        self._client: httpx.AsyncClient | None = None
        self._user: dict | None = None

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

    # -- OAuth ----------------------------------------------------------------

    async def _token_request(self, data: dict) -> dict:
        data = {**data, "client_id": CLIENT_ID}
        r = await self._http().post(f"{API_BASE}/oauth/token", data=data)
        try:
            body = r.json()
        except Exception:
            body = {}
        if r.status_code >= 400 or body.get("error"):
            # FN answers OAuth failures in the documented `error` /
            # `error_description` shape, but its *middleware* rejections come
            # back as `{"message": ...}` with a 422 — a different shape from a
            # different layer. Reading only the OAuth keys turned the single
            # most important failure into the useless string "HTTP 422".
            msg = (body.get("error_description") or body.get("error")
                   or body.get("message") or f"HTTP {r.status_code}")
            if "recaptcha" in str(msg).lower():
                # Written for whoever is looking at the Settings panel, not for
                # whoever is reading this file: an error that says "see the
                # module docstring" is no help to the person who has to act on
                # it, and this one is entirely actionable.
                raise FnRecaptchaError(
                    "FurryNetwork now requires a reCAPTCHA check on password login, "
                    "which no app can pass. Paste a refresh token instead: log in at "
                    "furrynetwork.com, then DevTools (F12) → Application → Local "
                    "Storage → furrynetwork.com → copy the refresh_token value."
                )
            raise FnAuthError(f"FurryNetwork auth failed: {msg}")
        self.access_token = body.get("access_token", "") or self.access_token
        self.refresh_token = body.get("refresh_token", "") or self.refresh_token
        # Renew a minute early to avoid a mid-request expiry.
        self._token_expiry = time.monotonic() + max(60, _safe_int(body.get("expires_in")) - 60)
        return body

    async def login(self) -> bool:
        """Obtain a token: refresh if we have one, else password grant."""
        if self.refresh_token:
            try:
                await self._token_request({"grant_type": "refresh_token",
                                           "refresh_token": self.refresh_token})
                return True
            except FnAuthError:
                # Refresh token dead → fall back to password grant if we can.
                if not (self.username and self.password):
                    raise
        if self.username and self.password:
            await self._token_request({"grant_type": "password",
                                       "username": self.username,
                                       "password": self.password})
            return True
        return False

    async def _ensure_token(self) -> None:
        if not self.access_token or time.monotonic() >= self._token_expiry:
            await self.login()

    async def _get(self, path: str, params: dict | None = None) -> Any:
        await self._ensure_token()
        r = await self._http().get(
            f"{API_BASE}/{path.lstrip('/')}", params=params,
            headers={"Authorization": f"Bearer {self.access_token}"})
        if r.status_code == 401:
            # Token may have died early — one forced refresh, then retry once.
            await self.login()
            r = await self._http().get(
                f"{API_BASE}/{path.lstrip('/')}", params=params,
                headers={"Authorization": f"Bearer {self.access_token}"})
        if r.status_code == 401:
            raise FnAuthError("FurryNetwork rejected the token (401)")
        if r.status_code >= 400:
            return None
        try:
            return r.json()
        except Exception:
            return None

    # -- User / characters ----------------------------------------------------

    async def get_user(self) -> dict | None:
        if self._user is None:
            self._user = await self._get("user") or None
        return self._user

    async def get_characters(self) -> list[dict]:
        user = await self.get_user()
        if not user:
            return []
        chars = user.get("characters") or []
        return [c for c in chars if isinstance(c, dict)]

    async def validate_session(self) -> str | None:
        """Confirm the credentials/token work. Returns the FN display name.

        Raises FnAuthError on a real auth failure so session-check shows the true
        reason; returns None only when nothing is configured.
        """
        if not (self.refresh_token or (self.username and self.password)):
            return None
        user = await self.get_user()
        if not user:
            return None
        # Prefer the account's own name; fall back to the login email.
        return user.get("email") or user.get("name") or self.username or "FurryNetwork"

    # -- Discovery ------------------------------------------------------------

    async def get_all_post_uris(self, types: tuple[str, ...] = ("artwork",)) -> list[dict]:
        """List the connected user's own submissions across all their characters.

        FN groups work under characters, so we page each character's gallery. The
        `search` endpoint carries full engagement data per hit, so no per-item
        fetch is needed — the raw hit is stashed for get_post_details_batch().
        """
        items: list[dict] = []
        seen: set[str] = set()
        for ch in await self.get_characters():
            name = ch.get("name")
            if not name:
                continue
            for t in types:
                frm = 0
                for _safety in range(200):        # 200*30 = 6k per char/type ceiling
                    data = await self._get("search", {
                        "character": name, "types[]": t, "sort": "created", "from": frm})
                    hits = _search_hits(data)
                    if not hits:
                        break
                    for h in hits:
                        sid = str(_safe_int(h.get("id")))
                        if not sid or sid in seen:
                            continue
                        seen.add(sid)
                        items.append({"post_uri": sid, "raw": h, "character": name})
                    if len(hits) < 30:
                        break
                    frm += len(hits)
        logger.info("FurryNetwork: found %d submissions across characters", len(items))
        return items

    async def get_post_details_batch(self, items: list[dict]) -> list[dict]:
        """Parse the raw hits gathered in discovery — no extra API calls."""
        return [self._parse_submission(it.get("raw") or {}, it.get("character", ""))
                for it in items]

    # -- Parsing --------------------------------------------------------------

    def _parse_submission(self, s: dict, character: str = "") -> dict:
        sid = str(_safe_int(s.get("id")))
        images = s.get("images") or {}
        file_url = images.get("original") or s.get("url") or ""
        thumb = (images.get("thumbnail") or images.get("small")
                 or images.get("medium") or file_url or "")
        tags = s.get("tags") or []
        keywords = [str(t.get("tag") if isinstance(t, dict) else t)
                    for t in tags if t]
        return {
            "post_uri": sid,
            "title": s.get("title") or f"#{sid}",
            "full_text": s.get("description", "") or "",
            "username": character or self.username,
            "posted_at": s.get("published") or s.get("created") or "",
            "content_type": "image",
            "rating": _RATING_FROM_FN.get(_safe_int(s.get("rating")), ""),
            "description": s.get("description", "") or "",
            "keywords": keywords,
            "link": f"{SITE_BASE}/{character}/artwork/{sid}" if character else f"{SITE_BASE}/artwork/{sid}",
            "thumbnail_url": thumb,
            "file_url": file_url,
            "views": _safe_int(s.get("views")),
            "favorites_count": _safe_int(s.get("favorites")),
            "comments_count": _safe_int(s.get("comments")),
            "has_media": 1 if file_url else 0,
        }

    async def get_follower_count(self) -> int | None:
        """Total followers across the account's characters (for the follower series)."""
        chars = await self.get_characters()
        if not chars:
            return None
        total = 0
        seen_any = False
        for c in chars:
            f = c.get("followers")
            if f is not None:
                total += _safe_int(f)
                seen_any = True
        return total if seen_any else None

    # -- Posting --------------------------------------------------------------

    async def upload_artwork(self, *, character: str, file_path: str, title: str,
                             description: str = "", tags: list[str] | None = None,
                             rating: str = "general", status: str = "public") -> dict:
        """Upload one artwork under `character`: chunked/resumable upload of the
        bytes, then PATCH the metadata. Returns {"success", "id", "url"}.

        Built to CrosspostSharp's flow; verify live before trusting in prod.
        """
        if not os.path.isfile(file_path):
            return {"success": False, "error": f"file not found: {file_path}"}
        await self._ensure_token()
        size = os.path.getsize(file_path)
        filename = os.path.basename(file_path)
        total_chunks = max(1, (size + UPLOAD_CHUNK - 1) // UPLOAD_CHUNK)
        identifier = f"{size}-{filename.replace('.', '')}"
        upload_path = f"{API_BASE}/submission/{character}/artwork/upload"
        headers = {"Authorization": f"Bearer {self.access_token}"}

        new_id = ""
        async with httpx.AsyncClient(timeout=UPLOAD_TIMEOUT) as up:
            with open(file_path, "rb") as fh:
                for chunk_no in range(1, total_chunks + 1):
                    blob = fh.read(UPLOAD_CHUNK)
                    params = {
                        "resumableChunkNumber": chunk_no,
                        "resumableChunkSize": UPLOAD_CHUNK,
                        "resumableCurrentChunkSize": len(blob),
                        "resumableTotalSize": size,
                        "resumableType": "application/octet-stream",
                        "resumableIdentifier": identifier,
                        "resumableFilename": filename,
                        "resumableRelativePath": filename,
                        "resumableTotalChunks": total_chunks,
                    }
                    r = await up.post(upload_path, params=params, content=blob,
                                      headers={**headers, "Content-Type": "application/octet-stream"})
                    if r.status_code >= 400:
                        return {"success": False,
                                "error": f"chunk {chunk_no}/{total_chunks} failed (HTTP {r.status_code})"}
                    # The final chunk's response carries the created submission.
                    if chunk_no == total_chunks:
                        try:
                            body = r.json()
                            new_id = str(body.get("id") or "")
                        except Exception:
                            new_id = ""

        if not new_id:
            return {"success": False, "error": "upload finished but no submission id returned"}

        # PATCH the metadata onto the freshly-uploaded artwork.
        fn_tags, dropped = sanitize_tags(tags)
        if dropped:
            logger.warning("FN: dropped %d tag(s) FurryNetwork will not accept: %s",
                           len(dropped), ", ".join(dropped))
        patch = {
            "title": title,
            "description": description,
            "tags": fn_tags,
            "rating": _RATING_TO_FN.get((rating or "general").lower(), 0),
            "status": status if status in ("draft", "unlisted", "public") else "public",
        }
        pr = await self._http().patch(
            f"{API_BASE}/artwork/{new_id}", json=patch,
            headers={**headers, "Content-Type": "application/json"})
        if pr.status_code >= 400:
            # Carry FN's own words. A bare "HTTP 422" says only that some field
            # was rejected, not which — and 422 is exactly the status that comes
            # with a body naming the field. Reporting the status alone made this
            # undiagnosable from the log, the same way the auth failure read as
            # "HTTP 422" until `message` was surfaced.
            try:
                detail = pr.text[:300]
            except Exception:
                detail = ""
            return {"success": False, "id": new_id,
                    # ⚠ The phrase "already uploaded" is load-bearing:
                    # `_schedule_retry` matches it to stop retrying. The upload
                    # half SUCCEEDED, so a retry re-uploads from scratch and
                    # leaves another draft on FurryNetwork — it can never fix
                    # the PATCH, only multiply the orphans.
                    "error": f"already uploaded (id {new_id}) but metadata PATCH failed "
                             f"(HTTP {pr.status_code}): {detail}"}
        url = f"{SITE_BASE}/{character}/artwork/{new_id}"
        return {"success": True, "id": new_id, "url": url}


def _search_hits(data: Any) -> list[dict]:
    """FN's search response shape isn't fully documented; tolerate the common
    envelopes — a bare list, {"hits": [...]}, or ES-style {"hits": {"hits": [...]}}."""
    if isinstance(data, list):
        return [h for h in data if isinstance(h, dict)]
    if isinstance(data, dict):
        hits = data.get("hits")
        if isinstance(hits, list):
            return [h.get("_source", h) if isinstance(h, dict) else h for h in hits]
        if isinstance(hits, dict) and isinstance(hits.get("hits"), list):
            return [h.get("_source", h) for h in hits["hits"] if isinstance(h, dict)]
        if isinstance(data.get("results"), list):
            return [h for h in data["results"] if isinstance(h, dict)]
    return []
