"""e621 (E621) official REST API client.

e621 exposes a first-class JSON API (https://e621.net/help/api). Auth is HTTP
Basic with your **username + API key** (Account → Manage API Access — NOT your
password). We poll the connected user's own uploads via the `user:<username>`
tag search and snapshot each post's engagement, and we can *upload* new posts
(POST /uploads.json) — see ``upload_post``.

Policy compliance (https://e621.net/help/api):
  - A descriptive, non-empty **User-Agent is mandatory**, and it MUST NOT
    impersonate a browser — doing so gets the client blocked. We send
    ``PawPoller/<version> (e621 analytics; user <name>)``.
  - Hard rate limit is 2 req/s; the docs ask for ~1 req/s best-effort. We sleep
    ``config.E621_REQUEST_DELAY_SECONDS`` (1.0s) between paged requests.

Response shape (future-proofing):
  /posts.json has two live formats. The default is the LEGACY one
  (``{"posts": [...]}`` with flat ``file`` / ``score`` / ``fav_count``), which
  e621's own OpenAPI marks *deprecated*. We request the supported v2 format
  (``v2=true&mode=extended`` → a bare array of nested ``files`` / ``stats``
  objects) and ``_parse_post`` tolerates BOTH shapes, so polling keeps working
  whichever one e621 returns.

Metric shape (normalised across both formats):
  - score  = score.total  (can be NEGATIVE — down-votes); up/down kept too
  - favorites_count = fav_count
  - comments_count  = comment_count
Post IDs are integers; we store them as TEXT strings.
"""

from __future__ import annotations
import asyncio
import logging
from typing import Any

import httpx

import config

logger = logging.getLogger(__name__)

_API_BASE = "https://e621.net"

_RATING_MAP = {"s": "Safe", "q": "Questionable", "e": "Explicit"}

# file extension → coarse content type (drives the type badge in the UI)
_EXT_TYPE = {
    "png": "image", "jpg": "image", "jpeg": "image", "webp": "image",
    "gif": "animation",
    "webm": "video", "mp4": "video",
    "swf": "flash",
}


def _safe_int(val: Any) -> int:
    if val is None:
        return 0
    try:
        if isinstance(val, str):
            val = val.replace(",", "").strip()
        return int(val)
    except (ValueError, TypeError):
        return 0


class E621Client:
    """Async client for the official e621 REST API."""

    def __init__(self, username: str = "", api_key: str = "",
                 proxy_url: str = "", proxy_key: str = ""):
        self.username = str(username or "").strip()
        self.api_key = str(api_key or "").strip()
        self._logged_in = False

        if proxy_url and proxy_key:
            from polling.cf_proxy import CloudflareProxyTransport
            transport = CloudflareProxyTransport(proxy_url, proxy_key)
            logger.info("e621 client using CF proxy: %s", proxy_url)
        else:
            transport = httpx.AsyncHTTPTransport(retries=2)
        self._http = httpx.AsyncClient(
            timeout=30.0,
            follow_redirects=True,
            transport=transport,
        )

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        await self.close()

    async def close(self) -> None:
        await self._http.aclose()

    def update_credentials(self, username: str, api_key: str) -> None:
        new_user = str(username or "").strip()
        new_key = str(api_key or "").strip()
        changed = (self.username != new_user or self.api_key != new_key)
        self.username = new_user
        self.api_key = new_key
        if changed:
            self._logged_in = False

    # -- HTTP -----------------------------------------------------------------

    def _headers(self) -> dict:
        # Descriptive, non-browser User-Agent (mandatory; impersonating a
        # browser is an explicit policy violation that gets the client blocked).
        who = self.username or "anonymous"
        return {
            "User-Agent": f"PawPoller/{config.APP_VERSION} (e621 self-analytics; user {who})",
            "Accept": "application/json",
        }

    def _auth(self):
        if self.username and self.api_key:
            return (self.username, self.api_key)
        return None

    async def _get_json(self, path: str, params: dict | None = None) -> Any:
        url = path if path.startswith("http") else f"{_API_BASE}{path}"
        try:
            resp = await self._http.get(url, params=params, headers=self._headers(), auth=self._auth())
            if resp.status_code in (401, 403):
                logger.warning("e621: auth rejected (%s) for %s", resp.status_code, path)
                return None
            if resp.status_code == 429:
                logger.warning("e621: rate limited (429), waiting 10s...")
                await asyncio.sleep(10)
                resp = await self._http.get(url, params=params, headers=self._headers(), auth=self._auth())
            if resp.status_code == 404:
                return None
            resp.raise_for_status()
            return resp.json()
        except httpx.HTTPError as e:
            logger.error("e621: failed to fetch %s: %s", path, e)
            return None
        except Exception as e:
            logger.error("e621: JSON parse error for %s: %s", path, e)
            return None

    # -- Comments (gap G3 inbox) ----------------------------------------------

    async def get_comments(self, post_id: str | int) -> list[dict]:
        """All comments on one of our posts (gap G3 inbox capture).

        One official ``GET /comments.json?search[post_id]=…`` call. Returns one
        dict per comment: {comment_id, author, body, commented_at, permalink}.
        Skips our own comments' capture filtering to the caller.
        """
        data = await self._get_json(
            "/comments.json",
            params={"search[post_id]": str(post_id), "limit": 75,
                    "search[order]": "id_asc"},
        )
        # e621 returns a bare list of comment objects (or {"comments": []} when
        # empty on some deployments) — tolerate both shapes.
        comments = data if isinstance(data, list) else (data or {}).get("comments", [])
        out = []
        for c in comments or []:
            cid = c.get("id")
            if cid is None:
                continue
            out.append({
                "comment_id": str(cid),
                "author": c.get("creator_name", "") or str(c.get("creator_id", "")),
                "body": c.get("body", ""),
                "commented_at": c.get("created_at", ""),
                "permalink": f"{_API_BASE}/posts/{post_id}#comment-{cid}",
            })
        return out

    async def post_comment(self, post_id: str | int, body: str) -> bool:
        """Post a comment (reply) on a post — gap G3 native reply.

        Official ``POST /comments.json`` with the same HTTP-Basic username +
        API-key auth the whole client uses. Returns True on success.
        """
        if not (self.username and self.api_key and body.strip()):
            return False
        try:
            resp = await self._http.post(
                f"{_API_BASE}/comments.json",
                data={"comment[post_id]": str(post_id), "comment[body]": body},
                headers=self._headers(), auth=self._auth(),
            )
            if resp.status_code in (200, 201):
                return True
            logger.warning("e621: comment post rejected (%s): %s",
                           resp.status_code, resp.text[:200])
            return False
        except httpx.HTTPError as e:
            logger.error("e621: comment post failed: %s", e)
            return False

    # -- Auth -----------------------------------------------------------------

    async def validate_session(self) -> str | None:
        """Confirm the username + API key work. Returns the username on success.

        Hits `/favorites.json` (an authenticated-only endpoint) — a wrong API
        key returns 401/403 there, whereas public post endpoints would 200
        regardless, so this actually verifies the key rather than the username.
        """
        if not self.username or not self.api_key:
            return None
        data = await self._get_json("/favorites.json", {"limit": 1})
        if data is None:
            return None
        self._logged_in = True
        return self.username

    async def ensure_logged_in(self) -> bool:
        if self._logged_in:
            return True
        return bool(await self.validate_session())

    # -- Post discovery -------------------------------------------------------

    async def get_all_post_uris(self) -> list[dict]:
        """Page through the connected user's own uploads (tags=user:<username>).

        e621 returns posts newest-first; deep pagination uses the `page=b<id>`
        (before-id) cursor rather than page numbers (which cap at 750). Each
        listing carries full engagement data, so no per-post fetch is needed —
        the raw post is stashed for get_post_details_batch() to parse.

        We request the v2 extended format (``v2=true&mode=extended``), which
        returns a bare array of posts with nested ``files`` / ``stats`` — the
        legacy ``{"posts": [...]}`` default is deprecated. Both envelopes are
        handled here and both post shapes in ``_parse_post``.
        """
        if not self.username:
            return []
        items: list[dict] = []
        seen: set[int] = set()
        before_id: int | None = None

        for _page_safety in range(500):  # 500 * 320 = 160k posts hard ceiling
            params: dict[str, Any] = {
                "tags": f"user:{self.username}", "limit": 320,
                "v2": "true", "mode": "extended",
            }
            if before_id is not None:
                params["page"] = f"b{before_id}"
            data = await self._get_json("/posts.json", params)
            # v2 extended → bare array; legacy default → {"posts": [...]}.
            if isinstance(data, list):
                page_posts = data
            elif isinstance(data, dict):
                page_posts = data.get("posts") or []
            else:
                break
            if not page_posts:
                break
            page_ids = []
            for p in page_posts:
                pid = _safe_int(p.get("id"))
                page_ids.append(pid)
                if not pid or pid in seen:
                    continue
                seen.add(pid)
                items.append({"post_uri": str(pid), "raw": p})
            before_id = min(page_ids) if page_ids else None
            if len(page_posts) < 320 or before_id is None:
                break
            await asyncio.sleep(config.E621_REQUEST_DELAY_SECONDS)

        logger.info("e621: found %d posts for user %s", len(items), self.username)
        return items

    async def get_post_details_batch(self, items: list[dict]) -> list[dict]:
        """Parse the raw posts gathered in discovery — no extra API calls."""
        details: list[dict] = []
        for item in items:
            raw = item.get("raw")
            detail = (self._parse_post(raw)
                      if raw else self._empty_detail(item.get("post_uri", "")))
            details.append(detail)
        return details

    # -- Parsing --------------------------------------------------------------
    #
    # Extraction helpers tolerate BOTH response shapes:
    #   v2 extended → nested  files.{original,preview,sample,meta}, stats.{...}
    #   legacy      → flat     file / preview / sample / score / fav_count

    @staticmethod
    def _file_url(p: dict) -> str:
        files = p.get("files")
        if isinstance(files, dict):
            return (files.get("original") or {}).get("url") or ""
        return (p.get("file") or {}).get("url") or ""

    @staticmethod
    def _file_ext(p: dict) -> str:
        files = p.get("files")
        if isinstance(files, dict):
            return ((files.get("meta") or {}).get("ext") or "").lower()
        return ((p.get("file") or {}).get("ext") or "").lower()

    @staticmethod
    def _thumb_url(p: dict) -> str:
        files = p.get("files")
        if isinstance(files, dict):
            preview = files.get("preview") or {}
            sample = files.get("sample") or {}
            return (preview.get("jpg") or preview.get("webp")
                    or sample.get("jpg") or sample.get("webp")
                    or (files.get("original") or {}).get("url") or "")
        preview = p.get("preview") or {}
        sample = p.get("sample") or {}
        return (preview.get("url") or sample.get("url")
                or (p.get("file") or {}).get("url") or "")

    @staticmethod
    def _stats(p: dict) -> tuple[int, int, int, int, int]:
        """Return (score_total, up, down, fav_count, comment_count) from either shape."""
        stats = p.get("stats")
        if isinstance(stats, dict):
            score = stats.get("score") or {}
            return (_safe_int(score.get("total")), _safe_int(score.get("up")),
                    _safe_int(score.get("down")), _safe_int(stats.get("fav_count")),
                    _safe_int(stats.get("comment_count")))
        score = p.get("score") or {}
        return (_safe_int(score.get("total")), _safe_int(score.get("up")),
                _safe_int(score.get("down")), _safe_int(p.get("fav_count")),
                _safe_int(p.get("comment_count")))

    def _parse_post(self, p: dict) -> dict:
        pid = str(_safe_int(p.get("id")))
        file_url = self._file_url(p)
        thumb = self._thumb_url(p)
        score_total, up_score, down_score, fav_count, comment_count = self._stats(p)

        ext = self._file_ext(p)
        content_type = _EXT_TYPE.get(ext, "image")

        # Flatten every tag category into one keyword list.
        keywords: list[str] = []
        tags = p.get("tags", {}) or {}
        if isinstance(tags, dict):
            for cat_tags in tags.values():
                if isinstance(cat_tags, list):
                    keywords.extend(str(t) for t in cat_tags)

        description = p.get("description", "") or ""
        # e621 posts have no title; derive a readable one from the description
        # first line, falling back to the post number.
        first_line = description.strip().splitlines()[0].strip() if description.strip() else ""
        title = (first_line[:80] if first_line else f"#{pid}")

        return {
            "post_uri": pid,
            "title": title,
            "full_text": description,
            "username": self.username,
            "posted_at": p.get("created_at", "") or "",
            "content_type": content_type,
            "rating": _RATING_MAP.get((p.get("rating") or "").lower(), ""),
            "description": description,
            "keywords": keywords,
            "link": f"{_API_BASE}/posts/{pid}",
            "thumbnail_url": thumb,
            "file_url": file_url,
            "score": score_total,
            "up_score": up_score,
            "down_score": down_score,
            "favorites_count": fav_count,
            "comments_count": comment_count,
            "has_media": 1 if file_url else 0,
        }

    def _empty_detail(self, uri: str) -> dict:
        return {
            "post_uri": uri,
            "title": f"#{uri}" if uri else "",
            "full_text": "",
            "username": self.username,
            "posted_at": "",
            "content_type": "image",
            "rating": "",
            "description": "",
            "keywords": [],
            "link": f"{_API_BASE}/posts/{uri}" if uri else "",
            "thumbnail_url": "",
            "file_url": "",
            "score": 0,
            "up_score": 0,
            "down_score": 0,
            "favorites_count": 0,
            "comments_count": 0,
            "has_media": 0,
        }

    # -- Uploads --------------------------------------------------------------

    async def upload_post(
        self,
        *,
        tag_string: str,
        rating: str,
        file_path: str = "",
        direct_url: str = "",
        source: str = "",
        description: str = "",
        parent_id: int | None = None,
    ) -> dict:
        """Upload a new post to e621 (``POST /uploads.json``, HTTP Basic auth).

        Exactly one of ``file_path`` / ``direct_url`` must be supplied (the API
        treats them as mutually exclusive). ``rating`` is a single letter —
        ``s`` (safe), ``q`` (questionable) or ``e`` (explicit). ``tag_string``
        is space-separated tags — e621 needs a real tag set, not one keyword.

        Returns ``{"success": True, "post_id", "location", "url"}`` on success.
        Raises ``RuntimeError`` carrying e621's own message when the upload is
        rejected — duplicate (412, includes the existing post's location),
        missing tags / bad rating (412), or insufficient permissions (403).
        """
        if not (self.username and self.api_key):
            raise RuntimeError("e621 upload needs a username + API key")
        if bool(file_path) == bool(direct_url):
            raise RuntimeError("e621 upload needs exactly one of file_path or direct_url")

        rating = (rating or "").strip().lower()[:1]
        if rating not in ("s", "q", "e"):
            raise RuntimeError(f"e621 rating must be s/q/e (got {rating!r})")
        tag_string = " ".join((tag_string or "").split())
        if not tag_string:
            raise RuntimeError("e621 upload requires at least one tag")

        data: dict[str, Any] = {
            "upload[tag_string]": tag_string,
            "upload[rating]": rating,
        }
        if source:
            data["upload[source]"] = source
        if description:
            data["upload[description]"] = description
        if parent_id:
            data["upload[parent_id]"] = str(parent_id)

        files = None
        fh = None
        try:
            if file_path:
                import mimetypes
                import os
                fname = os.path.basename(file_path)
                mime = mimetypes.guess_type(fname)[0] or "application/octet-stream"
                fh = open(file_path, "rb")
                files = {"upload[file]": (fname, fh, mime)}
            else:
                data["upload[direct_url]"] = direct_url

            url = f"{_API_BASE}/uploads.json"
            resp = await self._http.post(
                url, data=data, files=files,
                headers=self._headers(), auth=self._auth(),
                timeout=120.0,
            )
        finally:
            if fh is not None:
                fh.close()

        # Success — {success, location, post_id}
        if resp.status_code == 200:
            body = resp.json() if resp.content else {}
            pid = _safe_int(body.get("post_id"))
            return {
                "success": True,
                "post_id": str(pid) if pid else "",
                "location": body.get("location", "") or "",
                "url": f"{_API_BASE}/posts/{pid}" if pid else "",
            }

        # Rejection — surface e621's own reason/message where possible.
        msg = f"HTTP {resp.status_code}"
        try:
            body = resp.json()
            reason = (body.get("reason") or body.get("message")
                      or body.get("errors") or "")
            loc = body.get("location") or ""
            if reason:
                msg = str(reason)
            if loc:
                msg = f"{msg} (existing: {_API_BASE}{loc})"
        except Exception:
            if resp.text:
                msg = resp.text[:200]

        if resp.status_code == 403:
            raise RuntimeError(f"e621 denied the upload: {msg} "
                               "(check the account's upload permissions)")
        raise RuntimeError(f"e621 rejected the upload: {msg}")

    # -- Editing an existing post ---------------------------------------------

    #: The tag categories e621 splits a post's tags into on read. The edit form
    #: wants them back as one flat, space-separated string.
    _TAG_CATEGORIES = ("general", "artist", "contributor", "copyright",
                       "character", "species", "invalid", "meta", "lore")

    async def get_post(self, post_id: str | int) -> dict | None:
        """One post's current server-side state (``GET /posts/{id}.json``).

        Returns the bare post object — e621 wraps it as ``{"post": ...}`` — or
        None when the post is missing or unreadable. ``edit_post`` needs this
        because an e621 edit is a **three-way merge**, not an overwrite: the
        form sends both the new value and the ``old_`` value it was derived
        from, and e621 reconciles against anything that changed in between.
        """
        data = await self._get_json(f"/posts/{post_id}.json")
        if not isinstance(data, dict):
            return None
        post = data.get("post")
        if isinstance(post, dict):
            return post
        return data if "id" in data else None

    @classmethod
    def _flatten_tags(cls, post: dict) -> list[str]:
        """Every tag on a post, all categories, as one flat list."""
        tags = post.get("tags") or {}
        if isinstance(tags, str):                    # tolerate the older shape
            return tags.split()
        out: list[str] = []
        for cat in cls._TAG_CATEGORIES:
            out.extend(tags.get(cat) or [])
        return out

    @staticmethod
    def _normalise_tag(tag: str) -> str:
        """e621 tags are lower-case with underscores, never spaces."""
        return str(tag or "").strip().lower().replace(" ", "_")

    async def edit_post(
        self,
        post_id: str | int,
        *,
        tags: list[str] | None = None,
        tag_mode: str = "merge",
        rating: str | None = None,
        description: str | None = None,
        sources: list[str] | None = None,
        parent_id: int | None = None,
        edit_reason: str = "",
        current: dict | None = None,
    ) -> dict:
        """Edit an existing post's metadata (``PATCH /posts/{id}.json``).

        Same HTTP Basic username + API key as the rest of the client — e621
        needs no browser session for this. Only the fields passed are sent;
        anything left as None is not touched, so a caller with no sources to
        push cannot accidentally wipe the ones already on the post.

        WARNING: **there is no title.** An e621 post is tags + rating +
        description + sources + parent, and nothing else — verified against
        the live API, not inferred from the edit form. Callers must drop the
        title rather than fold it into the description, which would rewrite
        the visible caption on every sync.

        ``tag_mode`` decides what happens to tags that are on the post but not
        in ``tags``:

        - ``"merge"`` (default) — ours are added, theirs are kept. e621 tags
          are **communal**: janitors and other users retag posts. Deleting
          their work on every metadata sync is how an account gets noticed,
          and it is the same rule spec section 0-A1 already binds us to —
          never silently overwrite a hand-edited upload.
        - ``"replace"`` — the post ends up with exactly ``tags``. Only for a
          caller that has explicitly confirmed the removals.

        Either way an empty final tag set is refused: e621 reads a blank
        ``tag_string`` as "remove every tag", which would gut a live post.

        Returns ``{"success": True, "post_id", "url", "tags"}``. Raises
        ``RuntimeError`` carrying e621's own message when the edit is rejected.
        """
        if not (self.username and self.api_key):
            raise RuntimeError("e621 edit needs a username + API key")

        if current is None:
            current = await self.get_post(post_id)
        if not current:
            raise RuntimeError(
                f"e621: could not read post {post_id} — refusing to edit blind "
                "(the merge fields need its current state)"
            )

        if tag_mode not in ("merge", "replace"):
            raise RuntimeError(f"e621: unknown tag_mode {tag_mode!r}")

        data: dict[str, Any] = {}
        final_tags: list[str] = []

        if tags is not None:
            existing = self._flatten_tags(current)
            ours = [t for t in (self._normalise_tag(t) for t in tags) if t]
            if tag_mode == "merge":
                final_tags = list(existing)
                seen = set(existing)
                for t in ours:
                    if t not in seen:
                        final_tags.append(t)
                        seen.add(t)
            else:
                final_tags = list(dict.fromkeys(ours))
            if not final_tags:
                raise RuntimeError(
                    "e621: refusing to send an empty tag set — that strips "
                    "every tag off the live post"
                )
            data["post[tag_string]"] = " ".join(final_tags)
            data["post[old_tag_string]"] = " ".join(existing)

        if rating is not None:
            r = str(rating).strip().lower()[:1]
            if r not in ("s", "q", "e"):
                raise RuntimeError(f"e621 rating must be s/q/e (got {rating!r})")
            data["post[rating]"] = r
            data["post[old_rating]"] = str(current.get("rating", "") or "")

        if description is not None:
            data["post[description]"] = description
            data["post[old_description]"] = str(current.get("description", "") or "")

        if sources is not None:
            clean = [s.strip() for s in sources if str(s or "").strip()]
            data["post[source]"] = "\n".join(clean)
            data["post[old_source]"] = "\n".join(current.get("sources") or [])

        if parent_id is not None:
            data["post[parent_id]"] = str(parent_id)
            old_parent = (current.get("relationships") or {}).get("parent_id")
            data["post[old_parent_id]"] = "" if old_parent is None else str(old_parent)

        if not data:
            return {"success": True, "post_id": str(post_id), "unchanged": True,
                    "url": f"{_API_BASE}/posts/{post_id}", "tags": final_tags}

        # e621 shows this on the post's version history. An unexplained bulk
        # retag from a tool is exactly what draws a janitor's attention.
        data["post[edit_reason]"] = (edit_reason or "Metadata sync via PawPoller")[:250]

        url = f"{_API_BASE}/posts/{post_id}.json"
        resp = await self._http.patch(
            url, data=data, headers=self._headers(), auth=self._auth(), timeout=60.0,
        )
        if resp.status_code == 429:
            logger.warning("e621: rate limited (429) editing %s, waiting 10s...", post_id)
            await asyncio.sleep(10)
            resp = await self._http.patch(
                url, data=data, headers=self._headers(), auth=self._auth(), timeout=60.0,
            )

        if resp.status_code in (200, 201, 204):
            logger.info("e621: edited post %s (%d tags)", post_id, len(final_tags))
            return {
                "success": True,
                "post_id": str(post_id),
                "url": f"{_API_BASE}/posts/{post_id}",
                "tags": final_tags,
            }

        msg = f"HTTP {resp.status_code}"
        try:
            body = resp.json()
            reason = (body.get("reason") or body.get("message")
                      or body.get("errors") or "")
            if reason:
                msg = str(reason)
        except Exception:
            if resp.text:
                msg = resp.text[:200]

        if resp.status_code == 403:
            raise RuntimeError(f"e621 denied the edit: {msg} "
                               "(the post may be locked, or the account lacks permission)")
        if resp.status_code == 404:
            raise RuntimeError(f"e621: post {post_id} not found")
        raise RuntimeError(f"e621 rejected the edit: {msg}")
