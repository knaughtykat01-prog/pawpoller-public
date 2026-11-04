"""Toyhouse client — spec 062. Cookie session + HTML pages; no API.

The Newgrounds pattern (clients/ng/client.py): the browser sign-in's cookies are the credential, the
owner's own pages are read for the numbers, and the site's own upload form is driven for a post.

**The connection.** Toyhouse's Cloudflare bot check answers every plain scripted request with
``403 cf-mitigated: challenge`` ("Just a moment"). The AO3 transport (``curl_cffi`` presenting
current Chrome, spec 041) gets the real page (tested 29 Oct 2026). Operator's decision, 29 Oct: use it
(spec 062, Complexity Tracking). Without curl_cffi the client refuses to build — it never falls back
to plain httpx, which would only meet the bot check and read as "signed out".

**What was read on 29 Oct 2026** (research.md):

- Signed in: the navbar's Profile link ``<a class="nav-link" href="https://toyhou.se/{user}">`` with a
  ``fa-home`` icon. Absent when signed out.
- ``/{user}/art?page=N``: one ``.gallery-thumb`` per image — ``a.magnific-item[data-id]`` (the id), a hidden
  caption with ``a[data-id="image-{id}"] .th-favorite-count`` (favourites), the date, the character badges,
  and ``[image-comments-count]`` + ``[image-comments-lazy]`` (the comments URL).
- ``/{user}/characters?page=N``: one ``.character-thumb`` per character — ``/{id}.{slug}``, the name,
  ``.th-favorite-count``, ``a.thumb-character-stat.images`` and ``.comments``.
- Comments: ``div.comment`` > ``a#comment-{id}``, ``a.user-name-badge`` (author), ``.comment-body-{id}``,
  ``abbr.datetime[data-original-title]``.
- Upload: ``GET /~images/upload`` for ``_token``, then multipart ``POST https://upload.toyhou.se/~images/upload``.
  ❓ What the reply looks like after a real upload isn't known yet; the poster finds the new image in the
  art list instead of trusting it (FR-SEC-009).

**Safety (spec 062 FR-SEC-003/004/005).** Requests go only to ``toyhou.se`` and ``upload.toyhou.se``; redirects
are followed by hand and only on those hosts; URLs found inside pages are never fetched (the comments URL is
rebuilt from numeric ids). Page bodies, cookies and form tokens are never logged or put in an error.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
from html import unescape
from urllib.parse import urljoin, urlsplit

import httpx

try:
    from httpx_curl_cffi import AsyncCurlTransport
except Exception:  # noqa: BLE001 — a seat built without it says so below
    AsyncCurlTransport = None

logger = logging.getLogger(__name__)

BASE = "https://toyhou.se"
UPLOAD_BASE = "https://upload.toyhou.se"
_HOSTS = frozenset({"toyhou.se", "upload.toyhou.se"})
HTTP_TIMEOUT = 30.0
UPLOAD_TIMEOUT = 300.0
# FR-SEC-010: a hard stop on list pages. Pages hold 18 images (a tester's gallery read 30 Oct 2026, not the
# 24 first assumed) and a real gallery ran to 144 pages, so 40 cut it at 720. 300 pages is ~5,400 images,
# about 7½ minutes at PAGE_PAUSE_S, and only the weekly full read goes that far.
MAX_PAGES = 300
RECENT_PAGES = 5                # an ordinary poll and the poster: the newest ~90 images, where new activity lands
MAX_BODY = 4 * 1024 * 1024
MAX_REDIRECTS = 5
PAGE_PAUSE_S = 1.5              # between list pages — be a polite browser
COMMENT_MAX = 5000
IMAGE_TYPES = ("png", "jpg", "jpeg", "gif", "webp")
# ❓ Toyhouse doesn't publish an image cap; the 4 MB of the July notes is a stop, not a fact.
IMAGE_MAX_BYTES = 4 * 1024 * 1024

_USER_RE = re.compile(r"^[A-Za-z0-9_\-]{1,40}$")
_PROFILE_RE = re.compile(
    r'<a class="nav-link" href="https://toyhou\.se/([A-Za-z0-9_\-]{1,40})">\s*<span class="fa fa-home"></span>')
_TOKEN_RE = re.compile(r'<input[^>]*name="_token"[^>]*value="([^"]+)"|<input[^>]*value="([^"]+)"[^>]*name="_token"')
_THUMB_SPLIT_RE = re.compile(r'<div class="gallery-thumb thumb">')
_IMG_ID_RE = re.compile(r'class="magnific-item[^"]*"[^>]*data-id="(\d{1,15})"')
_IMG_FAV_RE = r'data-id="image-{id}"[^>]*>\s*<i[^>]*></i>\s*<span class="th-favorite-count">\s*(\d+)'
_COMMENTS_COUNT_RE = re.compile(r'image-comments-count="(\d+)"\s+image-comments-lazy="https://toyhou\.se/(\d{1,15})\.[^/"]*/(\d{1,15})/comments"')
_THUMB_SRC_RE = re.compile(r'<img[^>]*src="(https://f2\.toyhou\.se/file/f2-toyhou-se/thumbnails/[^"]+)"')
_POSTED_RE = re.compile(r'<div class="image-credits\s*">\s*<h2>\s*Credits\s*</h2>\s*<div class="mb-1">\s*([^<]+?)\s*</div>')
# The badge may carry a lock icon before the name (research R9) and an absolute link.
_CHAR_BADGE_RE = re.compile(r'<a href="(?:https://toyhou\.se)?/(\d{1,15})\.([A-Za-z0-9_\-]{0,80})" class="btn btn-sm btn-primary character-name-badge">\s*(?:<i[^>]*></i>)?\s*([^<]*?)\s*</a>')
_CHAR_CARD_SPLIT_RE = re.compile(r'<div class="text-center gallery-thumb character-thumb')
_CHAR_LINK_RE = re.compile(r'<a href="(?:https://toyhou\.se)?/(\d{1,15})\.([A-Za-z0-9_\-]{0,80})" class="img-thumbnail">')
_CHAR_NAME_RE = re.compile(r'<span class="thumb-character-name"><a[^>]*>(?:<i[^>]*></i>)?\s*([^<]*?)\s*</a>')
_CHAR_FAV_RE = re.compile(r'<span class="th-favorite-count">\s*(\d+)')
_CHAR_STAT_RE = r'class="thumb-character-stat {kind}"[^>]*>\s*<i[^>]*></i>\s*(\d+)'
# The owner's own pages add other query keys first (`folder:all?legacy=0&amp;page=2`, seen 4 Nov 2026): the picker
# stopped at 18 characters when this matched `?page=N` only. Any key order, `&` or `&amp;`, page=N ends the value.
_NEXT_PAGE_RE = r'href="(?:https://toyhou\.se)?/{path}\?(?:[^"#]*?&(?:amp;)?)?page={n}(?=["&])'
_COMMENT_SPLIT_RE = re.compile(r'<a id="comment-(\d{1,15})"></a>')
_COMMENT_AUTHOR_RE = re.compile(r'<a href="https://toyhou\.se/([A-Za-z0-9_\-]{1,40})" class="btn btn-sm btn-default user-name-badge')
_COMMENT_TIME_RE = re.compile(r'<abbr class="tooltipster datetime"[^>]*data-original-title="([^"]{1,40})"')
_TAG_RE = re.compile(r"<[^>]+>")
# A character page's sidebar (research R9): `<span class="… sidebar-stat sidebar-stat-gallery">12</span>`; Toyhouse
# spells the comments one "coments". 0 is an empty span or no span. The favourites span appears twice (a hidden
# "favourited" copy and the visible one) with the same number; the first is read.
_SIDEBAR_STAT_RE = r'class="[^"]*\bsidebar-stat-(?:{kinds})\b[^"]*"[^>]*>\s*([\d,]*)'
_SIDEBAR_STATS = {"favorites_count": "favorites", "comments_count": "coments|comments", "images_count": "gallery",
                  "links_count": "links"}


class THAuthError(Exception):
    """Signed out, or the session belongs to someone else."""


class THChallenged(Exception):
    """Toyhouse's bot check turned the request away (FR-SEC-008). Never a sign-in problem."""


class THUnavailable(Exception):
    """Toyhouse answered with an error (busy, rate-limited, down). Not a sign-in problem either (FR-002)."""


def _int(s) -> int:
    try:
        return int(str(s).replace(",", "").strip())
    except (TypeError, ValueError):
        return 0


def plain_text(html: str, limit: int = COMMENT_MAX) -> str:
    """HTML → plain text: paragraphs and breaks become newlines, every tag goes, entities are decoded."""
    s = re.sub(r"(?i)<br\s*/?>|</p>\s*<p[^>]*>", "\n", html or "")
    s = unescape(_TAG_RE.sub("", s))
    s = "\n".join(line.strip() for line in s.splitlines())
    return re.sub(r"\n{3,}", "\n\n", s).strip()[:limit]


def same_th_user(a: str, b: str) -> bool:
    return bool((a or "").strip()) and (a or "").strip().lower() == (b or "").strip().lower()


def logged_in_username(html: str) -> str:
    """The Profile link in the site's own header nav, and nowhere else: user-written HTML on profiles and
    characters could carry a lookalike link further down the page (FR-SEC-001)."""
    html = html or ""
    start = html.find('id="header"')
    end = html.find("</nav>", start)
    if start < 0 or end < 0:
        return ""
    m = _PROFILE_RE.search(html, start, end)
    return m.group(1) if m else ""


def is_challenge(status: int, headers, text: str) -> bool:
    return (status in (403, 503) and (bool(headers.get("cf-mitigated")) or "Just a moment" in (text or "")[:4000]))


def form_token(html: str) -> str:
    m = _TOKEN_RE.search(html or "")
    return (m.group(1) or m.group(2)) if m else ""


def parse_art_page(html: str) -> list[dict]:
    """The images on one ``/{user}/art`` page: id, favourites, comment count, the character it hangs under,
    a thumbnail and the posted date. Links are built from the ids, never copied from the page."""
    out = []
    for chunk in _THUMB_SPLIT_RE.split(html or "")[1:]:
        m = _IMG_ID_RE.search(chunk)
        if not m:
            continue
        iid = m.group(1)
        fav = re.search(_IMG_FAV_RE.format(id=iid), chunk)
        cc = _COMMENTS_COUNT_RE.search(chunk)
        chars = [(cid, slug, unescape(name)) for cid, slug, name in _CHAR_BADGE_RE.findall(chunk)]
        char_id = cc.group(2) if cc else (chars[0][0] if chars else "")
        slug = next((s for c, s, _ in chars if c == char_id), "")
        thumb = _THUMB_SRC_RE.search(chunk)
        posted = _POSTED_RE.search(chunk)
        out.append({
            "submission_id": iid,
            "title": ", ".join(n for _, _, n in chars)[:200] or f"Image {iid}",
            # None, not 0, where the layout carries no count (research R9): Toyhouse doesn't show it there.
            "favorites_count": _int(fav.group(1)) if fav else None,
            "comments_count": _int(cc.group(1)) if cc else None,
            "character_id": char_id,
            "character_ids": [c for c, _, _ in chars],
            "link": f"{BASE}/{char_id}.{slug or '-'}/{iid}" if char_id else f"{BASE}/~images/{iid}",
            "thumbnail_url": thumb.group(1) if thumb else "",
            "posted_at": unescape(posted.group(1)).strip() if posted else "",
        })
    return out


def parse_characters_page(html: str) -> list[dict]:
    """The characters on one characters page (folders are skipped: they have no id). An owner's own cards
    carry no counts (research R9): those come back None and are read from each character's page."""
    out = []
    for chunk in _CHAR_CARD_SPLIT_RE.split(html or "")[1:]:
        m = _CHAR_LINK_RE.search(chunk)
        if not m:
            continue
        name = _CHAR_NAME_RE.search(chunk)
        fav = _CHAR_FAV_RE.search(chunk)
        imgs = re.search(_CHAR_STAT_RE.format(kind="images"), chunk)
        com = re.search(_CHAR_STAT_RE.format(kind="comments"), chunk)
        out.append({
            "character_id": m.group(1),
            "name": plain_text(name.group(1), 200) if name else "",
            "link": f"{BASE}/{m.group(1)}.{m.group(2)}",
            "favorites_count": _int(fav.group(1)) if fav else None,
            "images_count": _int(imgs.group(1)) if imgs else None,
            "comments_count": _int(com.group(1)) if com else None,
        })
    return out


def parse_character_stats(html: str) -> dict | None:
    """A character's counts from its own page's sidebar, and only from there (FR-009a: an image's page carries
    its character's sidebar too, so these numbers are the character's, never an image's). None when the page
    has no sidebar (a content-warning page, for one): unknown, not zero."""
    html = html or ""
    start = html.find('id="sidebar"')
    if start < 0:
        return None
    end = html.find("content-main", start)
    side = html[start:end if end > 0 else start + 200_000]
    out = {}
    for key, kinds in _SIDEBAR_STATS.items():
        m = re.search(_SIDEBAR_STAT_RE.format(kinds=kinds), side)
        out[key] = _int(m.group(1)) if m and m.group(1) else 0
    return out


def parse_comments(html: str) -> list[dict]:
    """Every comment on a comments page (replies included), in the shape inbox_capture wants. The body is
    plain text and the permalink is built from the id (FR-SEC-004)."""
    parts = _COMMENT_SPLIT_RE.split(html or "")
    out = []
    for i in range(1, len(parts) - 1, 2):
        cid, chunk = parts[i], parts[i + 1]
        author = _COMMENT_AUTHOR_RE.search(chunk)
        when = _COMMENT_TIME_RE.search(chunk)
        body = re.search(r'comment-body-%s"[^>]*>(.*?)</div>' % cid, chunk, re.S)
        out.append({
            "comment_id": cid,
            "author": author.group(1) if author else "",
            "body": plain_text(body.group(1)) if body else "",
            "commented_at": when.group(1) if when else None,
            "permalink": f"{BASE}/~comments/{cid}",
        })
    return out


def has_next_page(html: str, path: str, n: int) -> bool:
    return bool(re.search(_NEXT_PAGE_RE.format(path=re.escape(path), n=n), html or "", re.I))


class THClient:
    def __init__(self, username: str = "", cookie: str = ""):
        self.username = (username or "").strip()
        self.cookie = (cookie or "").strip()
        self._client: httpx.AsyncClient | None = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        await self.close()

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    def _cookies(self) -> dict[str, str]:
        out: dict[str, str] = {}
        if "=" not in self.cookie:  # the guide's copy step gives the bare value
            return {"laravel_session": self.cookie} if self.cookie else out
        for part in self.cookie.split(";"):
            if "=" in part:
                k, v = part.split("=", 1)
                if k.strip():
                    out[k.strip()] = v.strip()
        return out

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            if AsyncCurlTransport is None:
                raise THChallenged("This install is missing the browser-matching connection Toyhouse needs "
                                   "(curl_cffi). Reinstall or update PawPoller.")
            jar = httpx.Cookies()
            for k, v in self._cookies().items():
                jar.set(k, v, domain=".toyhou.se")      # toyhou.se and upload.toyhou.se, nothing else
            self._client = httpx.AsyncClient(
                timeout=HTTP_TIMEOUT, follow_redirects=False, cookies=jar,
                transport=AsyncCurlTransport(impersonate="chrome", default_headers=True))
        return self._client

    @staticmethod
    def _host_ok(url: str) -> bool:
        parts = urlsplit(url)
        return parts.scheme == "https" and parts.hostname in _HOSTS and parts.port in (None, 443)

    async def _request(self, method: str, url: str, **kw) -> httpx.Response:
        """One request, redirects followed by hand and only on Toyhouse's own hosts (FR-SEC-003)."""
        for _ in range(MAX_REDIRECTS + 1):
            if not self._host_ok(url):
                raise ValueError("Toyhouse sent PawPoller somewhere else; not followed.")
            r = await self._http().request(method, url, **kw)
            if r.status_code in (307, 308) and method != "GET":
                return r        # would re-send an upload whose file has already been read: stop here
            if r.status_code in (301, 302, 303, 307, 308) and r.headers.get("location"):
                url = urljoin(url, r.headers["location"])
                if r.status_code in (301, 302, 303):
                    method, kw = "GET", {}
                continue
            if is_challenge(r.status_code, r.headers, r.text):
                raise THChallenged("Toyhouse's bot check turned PawPoller away.")
            return r
        raise ValueError("Toyhouse redirected too many times.")

    async def _get_text(self, path_or_url: str) -> str:
        url = path_or_url if path_or_url.startswith("https://") else BASE + path_or_url
        r = await self._request("GET", url)
        if r.status_code >= 400:
            # Loud, not empty: an empty art list before an upload would let the gallery check match an old
            # image, and an empty poll would read as "0 images" (implement review, spec 062).
            raise THUnavailable(f"Toyhouse didn't answer normally (HTTP {r.status_code}). Try again later.")
        return r.text[:MAX_BODY]

    # ── session ──────────────────────────────────────────────────────────────

    async def validate_session(self) -> dict:
        """{ok, logged_in, username, expected, matches, detail} — the Newgrounds shape. A bot check raises
        THChallenged instead (never "signed out")."""
        if not self.cookie:
            return {"ok": False, "logged_in": False, "username": "", "expected": self.username,
                    "matches": False, "detail": "No Toyhouse sign-in is saved."}
        r = await self._request("GET", f"{BASE}/~images/upload")      # no user content on this page
        if r.status_code >= 400:
            # Busy, rate-limited or down: only a page that loads without the Profile link means signed out.
            raise THUnavailable(f"Toyhouse didn't answer normally (HTTP {r.status_code}). Try again later.")
        who = logged_in_username(r.text[:MAX_BODY])
        if not who:
            return {"ok": False, "logged_in": False, "username": "", "expected": self.username, "matches": False,
                    "detail": "The saved sign-in has expired. Sign in to Toyhouse again."}
        matches = same_th_user(who, self.username) if self.username else False
        return {"ok": matches, "logged_in": True, "username": who, "expected": self.username, "matches": matches,
                "detail": "" if matches else f"The sign-in belongs to {who}, not {self.username}."}

    async def validate_cookies(self) -> bool:
        return bool((await self.validate_session()).get("ok"))

    # ── reading ──────────────────────────────────────────────────────────────

    def _user(self) -> str:
        if not _USER_RE.match(self.username):
            raise THAuthError("No Toyhouse username is saved for this account.")
        return self.username

    async def _all_pages(self, kind: str, parse, max_pages: int = MAX_PAGES) -> list[dict]:
        path = f"{self._user()}/{kind}"
        out, seen = [], set()
        for n in range(1, min(max_pages, MAX_PAGES) + 1):
            html = await self._get_text(f"/{path}" + (f"?page={n}" if n > 1 else ""))
            items = [i for i in parse(html) if (i.get("submission_id") or i.get("character_id")) not in seen]
            for i in items:
                seen.add(i.get("submission_id") or i.get("character_id"))
            out.extend(items)
            if not items or not has_next_page(html, path, n + 1):
                break
            await asyncio.sleep(PAGE_PAUSE_S)
        return out

    async def get_all_images(self, max_pages: int = MAX_PAGES) -> list[dict]:
        """The owner's art, newest first: every page by default, or the first ``max_pages``."""
        return await self._all_pages("art", parse_art_page, max_pages)

    async def get_characters(self, max_pages: int = MAX_PAGES) -> list[dict]:
        """Every character, from Toyhouse's own "all characters" view: the characters page itself holds only
        folder buttons, and folders nest (research R9)."""
        return await self._all_pages("characters/folder:all", parse_characters_page, max_pages)

    async def get_character_stats(self, character_id: str) -> dict | None:
        """One character's favourites, comments, images and links (its page's sidebar). None when the
        character is gone (404) or the page shows no sidebar; busy / down raises THUnavailable."""
        if not str(character_id).isdigit():
            return None
        r = await self._request("GET", f"{BASE}/{character_id}.-")
        if r.status_code == 404:
            return None
        if r.status_code >= 400:
            raise THUnavailable(f"Toyhouse didn't answer normally (HTTP {r.status_code}). Try again later.")
        return parse_character_stats(r.text[:MAX_BODY])

    async def get_image_comments(self, character_id: str, image_id: str) -> list[dict]:
        if not (str(character_id).isdigit() and str(image_id).isdigit()):
            return []
        return parse_comments(await self._get_text(f"/{character_id}.-/{image_id}/comments"))

    async def get_character_comments(self, character_id: str) -> list[dict]:
        if not str(character_id).isdigit():
            return []
        return parse_comments(await self._get_text(f"/{character_id}.-/comments"))

    async def get_follower_count(self) -> int | None:
        return None

    # ── posting ──────────────────────────────────────────────────────────────

    async def upload_image(self, *, file_path: str, caption: str, character_ids: list[str], flags: dict,
                           credits: list[dict], privacy: int = 0) -> dict:
        """Fill in and send the upload form. Returns {"sent": True, "status"} or {"sent": False, "error"}.
        The new image's id is found afterwards by the caller (FR-SEC-009)."""
        if not os.path.isfile(file_path):  # noqa: ASYNC240
            return {"sent": False, "error": "The image file is missing."}
        token = form_token(await self._get_text("/~images/upload"))
        if not token:
            return {"sent": False, "error": "Toyhouse's upload page didn't load as expected (signed out, or the page changed)."}
        data: list[tuple[str, str]] = [
            ("_token", token), ("referer_url", f"{BASE}/~images/upload"),
            ("caption", caption or ""), ("authorized_privacy", str(privacy)), ("public_privacy", str(privacy)),
            ("watermark_id", "1"), ("is_sexual", str(flags.get("is_sexual", 0))), ("warning", flags.get("warning", "")),
        ]
        for box in ("is_nudity", "is_gore", "is_sensitive"):
            if flags.get(box):
                data.append((box, "1"))
        for cid in character_ids:
            data.append(("character_ids[]", str(cid)))
        for c in credits:
            onsite = c.get("type") == "onsite"
            data += [("artist[]", "onsite" if onsite else "offsite"),
                     ("artist_username[]", c.get("username", "") if onsite else ""),
                     ("artist_url[]", "" if onsite else c.get("url", "")),
                     ("artist_name[]", "" if onsite else c.get("name", "")),
                     ("artist_credit[]", c.get("role", ""))]
        ext = os.path.splitext(file_path)[1].lstrip(".").lower()
        mime = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png", "gif": "image/gif",
                "webp": "image/webp"}.get(ext, "application/octet-stream")
        form: dict[str, list[str]] = {}          # httpx wants repeated fields (character_ids[]…) as lists
        for k, v in data:
            form.setdefault(k, []).append(v)
        with open(file_path, "rb") as fh:  # noqa: ASYNC230
            r = await self._request("POST", f"{UPLOAD_BASE}/~images/upload", data=form,
                                    files={"image": (os.path.basename(file_path), fh, mime)},
                                    headers={"Referer": f"{BASE}/~images/upload", "Origin": BASE},
                                    timeout=UPLOAD_TIMEOUT)
        if r.status_code >= 500:
            # A gateway error on a slow upload often means the image was saved anyway: the poster checks the
            # gallery rather than reporting a failure that a retry would turn into a double post (FR-SEC-009).
            return {"sent": True, "status": r.status_code}
        if r.status_code >= 400:
            return {"sent": False, "error": f"Toyhouse refused the upload (HTTP {r.status_code})."}
        if 'name="_token"' in r.text and "/~images/upload" in r.text and "has-danger" in r.text:
            return {"sent": False, "error": "Toyhouse sent the upload form back with an error."}
        return {"sent": True, "status": r.status_code}
