"""Newgrounds client — MEDIAPLATS §5 (4.23.0). Cookie session + HTML forms; no API.

Newgrounds has no submission API (newgrounds.io is medals / scoreboards for games), so this
is the FurAffinity pattern: a browser-login cookie session, the operator's own pages scraped
for the numbers, and the site's project forms driven for a post.

**What was read from the live public pages on 2026-09-08** (a real browser; the site fronts
every fetch with an "NG Guard" JS challenge, so plain HTTP may see that page first — ❓ probe
from the VM):

- A user's listings: ``https://{user}.newgrounds.com/audio`` renders
  ``<a href="https://www.newgrounds.com/audio/listen/{id}" class="item-audiosubmission" title="…">``
  and ``/movies`` renders ``<a href="https://www.newgrounds.com/portal/view/{id}"
  class="inline-card-portalsubmission" title="…">`` with a ``nohue-ngicon-small-rated-{e|t|m|a}``
  badge and a ``title="Score: 4.55/5.00"`` star. Small galleries fit one page; ❓ larger ones page
  with ``?page=N`` (assumed — stop when a page adds no new id).
- An item page: ``<h2 class="rated-{e|t|m|a}" itemprop="name">Title</h2>`` and
  ``<div id="sidestats"><dl class="sidestats single-row" data-statistics="…"><dt>Listens|Views</dt>
  <dd>302,940</dd><dt>Faves</dt><dd><a …>400</a></dd><dt>Downloads</dt><dd>80,461</dd><dt>Votes</dt>
  <dd>2,521</dd><dt>Score</dt><dd>… <span id="score_number">4.45</span> / 5.00``, then
  ``<dt>Uploaded</dt>`` with two ``<span class="value">`` (date, time), ``<dt>Genre</dt>``, and the
  ``og:*`` metas (``og:image`` = the icon, ``og:audio`` = the mp3). Reviews have no count in the
  stats block — ``comments_count`` stays 0 (❓ the reviews pager says "Page 1 of N", not a count).
- The profile page: ``<span>FANS</span><strong>562</strong>`` — the follower series.

**Posting** is PostyBirb's art-portal flow (``apps/…/websites/implementations/newgrounds``,
read 2026-09-08) transposed to the audio and movie portals — every portal-specific name below
is marked ❓ until the first live submission reads the real form:

1. ``GET /projects/{portal}/new`` → the page's ``PHP.merge({... "uek": "<userkey>" ...})``.
2. ``POST /projects/{portal}/new`` multipart ``init_project=1, userkey`` → JSON
   ``{project_id, edit_url, success: "saved", can_publish}``.
3. ``POST edit_url`` multipart with ``userkey`` + the file field (art uses ``new_image``; ❓ audio
   ``new_audio``, movie ``new_movie``) and the icon (``thumbnail``); then ``option[longdescription]``
   (``encoder=quill``), then one field per request — ``title``, ``option[tags]`` (comma list,
   lowercase, spaces → ``-``, 12 max), ``option[genreid]``, the four content descriptors
   ``option[nudity|violence|language_textual|adult_themes]`` each ``a`` (lots) / ``b`` (some) /
   ``c`` (none), which is how Newgrounds derives E / T / M / A, ``option[include_in_portal]=1``;
   a key ending ``_error`` in a response is the site refusing a field.
4. ``POST /projects/{portal}/{id}/publish`` with ``submit=1, agree=Y, __ng_design=2015`` — the
   piece enters **Under Judgment**; the response URL is the public page.
   On any failure ``POST /projects/{portal}/remove/{id}`` cleans the draft up.

The 44.1 kHz rule for mp3 cannot be checked here (no decoding); the refusal, if any, is the
site's own ``_error`` text.
"""
from __future__ import annotations

import logging
import os
import re
from typing import Any

import httpx

logger = logging.getLogger(__name__)

BASE = "https://www.newgrounds.com"
HTTP_TIMEOUT = 30.0
UPLOAD_TIMEOUT = 900.0
MAX_PAGES = 50
USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
              "Chrome/128.0 Safari/537.36")

# The three lists are the site's own submit menu (read 2026-09-09): AUDIO "mp3, m4a, ogg",
# ANIMATION "swf, mp4, mov, wmv", ART "webp, gif, jpg, png". swf is not a thing the Library makes.
AUDIO_TYPES = ("mp3", "m4a", "ogg")                                 # Audio Portal (mp3 at 44.1 kHz)
MOVIE_TYPES = ("mp4", "mov", "wmv")                                 # Movie Portal
ART_TYPES = ("png", "jpg", "jpeg", "gif", "webp")                   # Art Portal (4.29.0)
AUDIO_MAX_BYTES = 250 * 1024 * 1024
MOVIE_MAX_BYTES = 400 * 1024 * 1024                                 # ❓ not published; a stop, not a fact
ART_MAX_BYTES = 40 * 1024 * 1024                                    # ❓ not published; a stop, not a fact

# Portal-specific names. The ART row is PostyBirb's proven flow (its only Newgrounds portal);
# ❓ audio and movie are by analogy with it. Pinned here so the first live run changes one
# table, not four files. An art piece's public URL is /art/view/<user>/<slug>, which only the
# publish redirect knows — so "view" for art is a fallback, and a stored art id is that path.
PORTALS = {
    "audio": {"new": "/projects/audio/new", "file_field": "new_audio", "remove": "/projects/audio/remove/{id}",
              "publish": "/projects/audio/{id}/publish", "view": "/audio/listen/{id}", "edit": "/projects/audio/{id}/edit",
              "listing": "audio"},
    "movie": {"new": "/projects/movies/new", "file_field": "new_movie", "remove": "/projects/movies/remove/{id}",
              "publish": "/projects/movies/{id}/publish", "view": "/portal/view/{id}", "edit": "/projects/movies/{id}/edit",
              "listing": "movies"},
    "art":   {"new": "/projects/art/new", "file_field": "new_image", "remove": "/projects/art/remove/{id}",
              "publish": "/projects/art/{id}/publish", "view": "/art/view/{id}", "edit": "/projects/art/{id}/edit",
              "listing": "art"},
}

# The four descriptors Newgrounds derives its E / T / M / A rating from.
DESCRIPTORS = ("nudity", "violence", "language_textual", "adult_themes")
RATING_DESCRIPTORS = {"general": "c", "mature": "b", "adult": "a"}

# The audio genres Newgrounds lists (id → name), as read from the browse filter on 2026-09-08 for
# the ones visible on public pages; ❓ the complete list is on the submit form.
AUDIO_GENRES = {
    "": "—", "1": "Ambient", "2": "Classical", "3": "Cinematic", "4": "Dance", "5": "Drum & Bass",
    "6": "Dubstep", "7": "Experimental", "8": "Funk", "9": "Fusion", "10": "Techno", "11": "Hip Hop - Modern",
    "12": "House", "13": "Industrial", "14": "Jazz", "15": "Latin", "16": "Miscellaneous", "17": "New Wave",
    "18": "Pop", "19": "Rock", "20": "Trance", "21": "Video Game", "22": "Voice Demo", "23": "Podcast",
}
MOVIE_GENRES = {
    "": "—", "1": "Action", "2": "Comedy - Original", "3": "Comedy - Parody", "4": "Drama",
    "5": "Experimental", "6": "Informative", "7": "Music Video", "8": "Other", "9": "Spam",
}

_ITEM_ID_RE = {
    "audio": re.compile(r'href="https?://www\.newgrounds\.com/audio/listen/(\d+)"[^>]*class="item-audiosubmission[^"]*"[^>]*title="([^"]*)"'),
    "movie": re.compile(r'href="https?://www\.newgrounds\.com/portal/view/(\d+)"[^>]*class="inline-card-portalsubmission[^"]*"[^>]*title="([^"]*)"'),
    # ❓ the /art listing's card, by analogy with the other two; the id is the "user/slug" path.
    "art":   re.compile(r'href="https?://www\.newgrounds\.com/art/view/([^"/]+/[^"/?#]+)"[^>]*class="[^"]*item-portalitem-art[^"]*"[^>]*title="([^"]*)"'),
}
_TITLE_RE = re.compile(r'<h2 class="rated-([etma])"[^>]*itemprop="name"[^>]*>(.*?)</h2>', re.S)
_STAT_RE = r'<dt>\s*{label}\s*</dt>\s*<dd>\s*(?:<a[^>]*>\s*)?([\d,]+)'
_SCORE_RE = re.compile(r'id="score_number">\s*([\d.]+)')
_UPLOADED_RE = re.compile(r'<dt[^>]*>\s*Uploaded\s*</dt>\s*<dd[^>]*>\s*<span class="value">\s*([^<]+?)\s*</span>\s*<span class="value">\s*([^<]+?)\s*</span>', re.S)
_GENRE_RE = re.compile(r'<dt>\s*Genre\s*</dt>\s*<dd>\s*<a[^>]*>([^<]+)</a>', re.S)
_OG_RE = r'property="og:{name}"\s+content="([^"]*)"'
_FANS_RE = re.compile(r'<span>\s*FANS\s*</span>\s*<strong>\s*([\d,]+)', re.I)
_ACTIVE_USER_RE = re.compile(r'"name"\s*:\s*"([^"]+)"')
_UEK_RE = re.compile(r'"uek"\s*:\s*"([^"]+)"')
_TAG_CLEAN_RE = re.compile(r"[()\:#;\[\]']")


class NgAuthError(Exception):
    """The cookie session is not signed in (or not as the account it should be)."""


def _safe_int(v: Any) -> int:
    try:
        return int(str(v or "0").replace(",", ""))
    except (TypeError, ValueError):
        return 0


def _strip_tags(s: str) -> str:
    return re.sub(r"<[^>]+>", "", s or "").strip()


def _unescape(s: str) -> str:
    import html
    return html.unescape(s or "")


def ng_tags(tags: list[str] | None, limit: int = 12) -> list[str]:
    """Newgrounds' tag rules as PostyBirb applies them: lowercase, no ( ) : # ; [ ] ', spaces → -."""
    out: list[str] = []
    for t in tags or []:
        t = _TAG_CLEAN_RE.sub("", str(t or "")).strip().replace("_", "-").replace(" ", "-").lower()
        if t and t not in out:
            out.append(t)
    return out[:limit]


def descriptors_for(rating: str, extra: dict | None = None) -> dict[str, str]:
    """The four content descriptors for a PawPoller rating; ``extra["ng_<descriptor>"]`` overrides one."""
    level = RATING_DESCRIPTORS.get((rating or "general").lower(), "c")
    out = {d: level for d in DESCRIPTORS}
    for d in DESCRIPTORS:
        v = str((extra or {}).get(f"ng_{d}", "") or "").lower()
        if v in ("a", "b", "c"):
            out[d] = v
    return out


def portal_for(kind: str, ext: str = "") -> str | None:
    """'audio' | 'movie' | 'art' for a media kind (or an extension), else None."""
    k = (kind or "").lower()
    e = (ext or "").lower().lstrip(".")
    if k == "audio" or e in AUDIO_TYPES:
        return "audio"
    if k == "video" or e in MOVIE_TYPES:
        return "movie"
    if k == "image" or e in ART_TYPES:
        return "art"
    return None


def parse_listing(html: str, portal: str) -> list[dict]:
    """[{submission_id, title}] from a user's audio / movies listing page, in page order."""
    seen, out = set(), []
    for sid, title in _ITEM_ID_RE[portal].findall(html or ""):
        if sid not in seen:
            seen.add(sid)
            out.append({"submission_id": sid, "title": _unescape(title)})
    return out


def parse_item(html: str, submission_id: str, portal: str, username: str = "") -> dict:
    """An item page in the ``ng_submissions`` row shape."""
    m = _TITLE_RE.search(html or "")
    rating = m.group(1) if m else ""
    title = _unescape(_strip_tags(m.group(2))) if m else ""

    def stat(label: str) -> int:
        mm = re.search(_STAT_RE.format(label=label), html or "", re.S)
        return _safe_int(mm.group(1)) if mm else 0

    def og(name: str) -> str:
        mm = re.search(_OG_RE.format(name=name), html or "")
        return _unescape(mm.group(1)) if mm else ""

    sm = _SCORE_RE.search(html or "")
    um = _UPLOADED_RE.search(html or "")
    gm = _GENRE_RE.search(html or "")
    return {
        "submission_id": str(submission_id),
        "portal": portal,
        "title": title or og("title"),
        "description": og("description"),
        "username": username,
        "rating": rating,
        "genre": _unescape(gm.group(1)).strip() if gm else "",
        "link": f"{BASE}{PORTALS[portal]['view'].format(id=submission_id)}",
        "thumbnail_url": og("image"),
        "posted_at": f"{um.group(1)} {um.group(2)}".strip() if um else "",
        "views": stat("Listens") or stat("Views"),
        "favorites_count": stat("Faves"),
        "comments_count": 0,
        "downloads_count": stat("Downloads"),
        "votes": stat("Votes"),
        "score": float(sm.group(1)) if sm else 0.0,
    }


def parse_fans(html: str) -> int | None:
    m = _FANS_RE.search(html or "")
    return _safe_int(m.group(1)) if m else None


def logged_in_username(html: str) -> str:
    """PostyBirb's check: a signed-in page carries ``activeuser`` and ``"name":"<user>"``."""
    if "activeuser" not in (html or ""):
        return ""
    m = _ACTIVE_USER_RE.search(html)
    return m.group(1).strip() if m else ""


def same_ng_user(a: str, b: str) -> bool:
    norm = lambda x: (x or "").strip().lower().replace("-", "").replace("_", "")
    return bool(norm(a)) and norm(a) == norm(b)


class NgClient:
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
        for part in self.cookie.split(";"):
            if "=" in part:
                k, v = part.split("=", 1)
                out[k.strip()] = v.strip()
        return out

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=HTTP_TIMEOUT, follow_redirects=True,
                                             headers={"User-Agent": USER_AGENT, "Accept": "text/html,*/*"},
                                             cookies=self._cookies())
        return self._client

    async def _get_text(self, url: str) -> str:
        r = await self._http().get(url)
        if r.status_code >= 400:
            return ""
        return r.text

    # ── session ──────────────────────────────────────────────────────────────

    async def validate_session(self) -> dict:
        """{ok, logged_in, username, expected, matches, detail} — the 3.31.0 / 4.6.3 shape."""
        if not self.cookie:
            return {"ok": False, "logged_in": False, "username": "", "expected": self.username,
                    "matches": False, "detail": "No Newgrounds cookie is saved."}
        html = await self._get_text(f"{BASE}/")
        who = logged_in_username(html)
        if not who:
            return {"ok": False, "logged_in": False, "username": "", "expected": self.username, "matches": False,
                    "detail": "The cookie is not signed in (or the site answered with its bot guard)."}
        matches = same_ng_user(who, self.username) if self.username else True
        return {"ok": matches, "logged_in": True, "username": who, "expected": self.username, "matches": matches,
                "detail": "" if matches else f"The session belongs to {who}, not {self.username}."}

    async def validate_cookies(self) -> bool:
        return bool((await self.validate_session()).get("ok"))

    # ── discovery ────────────────────────────────────────────────────────────

    def _user_base(self) -> str:
        return f"https://{self.username.lower()}.newgrounds.com"

    async def get_all_items(self, portal: str) -> list[dict]:
        """Every id in the operator's audio or movies listing (``?page=N`` until nothing new)."""
        out, seen = [], set()
        for page in range(1, MAX_PAGES + 1):
            url = f"{self._user_base()}/{PORTALS[portal]['listing']}"
            if page > 1:
                url += f"?page={page}"
            items = parse_listing(await self._get_text(url), portal)
            fresh = [i for i in items if i["submission_id"] not in seen]
            if not fresh:
                break
            for i in fresh:
                seen.add(i["submission_id"])
                i["portal"] = portal
                out.append(i)
            if len(items) < 10:
                break
        return out

    async def get_item(self, submission_id: str, portal: str) -> dict | None:
        url = f"{BASE}{PORTALS[portal]['view'].format(id=submission_id)}"
        html = await self._get_text(url)
        if not html or "sidestats" not in html:
            return None
        return parse_item(html, submission_id, portal, self.username)

    async def get_follower_count(self) -> int | None:
        if not self.username:
            return None
        return parse_fans(await self._get_text(f"{self._user_base()}/"))

    # ── posting ──────────────────────────────────────────────────────────────

    async def _post_json(self, url: str, data: dict, files: dict | None = None, timeout: float | None = None) -> dict:
        client = self._http()
        r = await client.post(url, data=data, files=files or None,
                              headers={"X-Requested-With": "XMLHttpRequest", "Referer": url},
                              timeout=timeout or HTTP_TIMEOUT)
        try:
            body = r.json()
        except Exception:
            body = {"_status": r.status_code, "_text": r.text[:300]}
        if isinstance(body, dict):
            body.setdefault("_status", r.status_code)
        return body if isinstance(body, dict) else {"_status": r.status_code, "_body": body}

    async def submit_project(self, portal: str, *, file_path: str, title: str, description: str = "",
                             tags: list[str] | None = None, genre_id: str = "", rating: str = "general",
                             icon_path: str | None = None, extra: dict | None = None) -> dict:
        """The whole project flow for one portal. Returns {"success", "id", "url"} or {"success": False, "error"}."""
        spec = PORTALS.get(portal)
        if not spec:
            return {"success": False, "error": f"unknown Newgrounds portal {portal!r}"}
        if not os.path.isfile(file_path):
            return {"success": False, "error": f"file not found: {file_path}"}
        page = await self._get_text(f"{BASE}{spec['new']}")
        m = _UEK_RE.search(page)
        if not m:
            return {"success": False, "error": "Could not read the Newgrounds user key from the submit page "
                                               "(signed out, or the bot guard answered)"}
        userkey = m.group(1)
        init = await self._post_json(f"{BASE}{spec['new']}", {"PHP_SESSION_UPLOAD_PROGRESS": "projectform",
                                                               "init_project": "1", "userkey": userkey})
        project_id, edit_url = init.get("project_id"), init.get("edit_url", "")
        if not project_id or init.get("success") != "saved" or not edit_url:
            return {"success": False, "error": f"Newgrounds would not start a project: {init}"}
        if edit_url.startswith("/"):
            edit_url = BASE + edit_url

        async def fail(stage: str, body: dict) -> dict:
            try:
                await self._post_json(f"{BASE}{spec['remove'].format(id=project_id)}", {"userkey": userkey})
            except Exception:
                logger.debug("NG: could not remove the failed project %s", project_id, exc_info=True)
            errs = "; ".join(str(v) for k, v in body.items() if str(k).endswith("_error"))
            return {"success": False, "id": str(project_id),
                    "error": f"Newgrounds {stage} failed: {errs or body}"}

        # the file (+ icon). The Art Portal (4.29.0, PostyBirb's proven flow) also wants the
        # image's size, `link_icon=1` with a crop box, and afterwards the returned `linked_icon`
        # sorted into the project (`art_image_sort`).
        files: dict[str, Any] = {}
        form: dict[str, str] = {"userkey": userkey}
        if portal == "art":
            w, h = _image_size(file_path)
            form.update({"width": str(w), "height": str(h), "link_icon": "1",
                         "cropdata": '{"x":0,"y":0,"width":%d,"height":%d}' % (w, h)})
        fh = open(file_path, "rb")
        files[spec["file_field"]] = (os.path.basename(file_path), fh, "application/octet-stream")
        fh_icon = None
        if icon_path and os.path.isfile(icon_path):
            fh_icon = open(icon_path, "rb")
            files["thumbnail"] = (os.path.basename(icon_path), fh_icon, "image/jpeg")
        try:
            up = await self._post_json(edit_url, form, files=files, timeout=UPLOAD_TIMEOUT)
        finally:
            fh.close()
            if fh_icon:
                fh_icon.close()
        if up.get("success") != "saved":
            return await fail("upload", up)
        if portal == "art":
            linked = up.get("linked_icon")
            if linked in (None, ""):
                return await fail("upload (no linked_icon came back)", up)
            sort = await self._post_json(edit_url, {"userkey": userkey, "art_image_sort": f"[{linked}]"})
            if sort.get("success") != "saved":
                return await fail("image sort", sort)

        # the description, then one field per request (the site's own form does this)
        desc = await self._post_json(edit_url, {"PHP_SESSION_UPLOAD_PROGRESS": "projectform", "userkey": userkey,
                                                "encoder": "quill", "option[longdescription]": _as_paragraphs(description)})
        if desc.get("success") != "saved":
            return await fail("description", desc)
        fields = {"title": title, "option[tags]": ",".join(ng_tags(tags)), "option[include_in_portal]": "1"}
        if genre_id:
            fields["option[genreid]"] = str(genre_id)
        for d, level in descriptors_for(rating, extra).items():
            fields[f"option[{d}]"] = level
        last: dict = {}
        for k, v in fields.items():
            last = await self._post_json(edit_url, {"PHP_SESSION_UPLOAD_PROGRESS": "projectform", "userkey": userkey, k: v})
            if last.get("success") != "saved" or any(str(kk).endswith("_error") for kk in last):
                return await fail(f"field {k}", last)
        if not last.get("can_publish"):
            return await fail("publish check (the project is missing something the site wants)", last)

        r = await self._http().post(f"{BASE}{spec['publish'].format(id=project_id)}",
                                    data={"userkey": userkey, "submit": "1", "agree": "Y", "__ng_design": "2015"},
                                    headers={"Referer": edit_url})
        if r.status_code >= 400:
            return await fail("publish", {"_status": r.status_code, "_text": r.text[:300]})
        url = str(r.url) if r.url else f"{BASE}{spec['view'].format(id=project_id)}"
        return {"success": True, "id": str(project_id), "url": url, "under_judgment": True}

    async def update_project(self, portal: str, project_id: str, *, title: str | None = None,
                             description: str | None = None, tags: list[str] | None = None,
                             genre_id: str | None = None, rating: str | None = None,
                             extra: dict | None = None) -> dict:
        """Edit a published piece through its project edit page (❓ the same fields as the submit form)."""
        spec = PORTALS.get(portal)
        if not spec:
            return {"success": False, "error": f"unknown Newgrounds portal {portal!r}"}
        edit_url = f"{BASE}{spec['edit'].format(id=project_id)}"
        page = await self._get_text(edit_url)
        m = _UEK_RE.search(page)
        if not m:
            return {"success": False, "error": "Could not read the Newgrounds user key from the edit page"}
        userkey = m.group(1)
        fields: dict[str, str] = {}
        if title is not None:
            fields["title"] = title
        if tags is not None:
            fields["option[tags]"] = ",".join(ng_tags(tags))
        if genre_id:
            fields["option[genreid]"] = str(genre_id)
        if rating is not None:
            for d, level in descriptors_for(rating, extra).items():
                fields[f"option[{d}]"] = level
        if description is not None:
            r = await self._post_json(edit_url, {"PHP_SESSION_UPLOAD_PROGRESS": "projectform", "userkey": userkey,
                                                 "encoder": "quill", "option[longdescription]": _as_paragraphs(description)})
            if r.get("success") != "saved":
                return {"success": False, "error": f"Newgrounds refused the description: {r}"}
        for k, v in fields.items():
            r = await self._post_json(edit_url, {"PHP_SESSION_UPLOAD_PROGRESS": "projectform", "userkey": userkey, k: v})
            if r.get("success") != "saved":
                return {"success": False, "error": f"Newgrounds refused {k}: {r}"}
        return {"success": True, "id": str(project_id), "url": f"{BASE}{spec['view'].format(id=project_id)}"}


def _image_size(path: str) -> tuple[int, int]:
    """(width, height) of an image file, (0, 0) when it cannot be read."""
    try:
        from PIL import Image
        with Image.open(path) as im:
            return int(im.width), int(im.height)
    except Exception:
        return 0, 0


def _as_paragraphs(text: str) -> str:
    """Plain text → the <p> markup the project form's Quill editor stores."""
    import html as _html
    paras = [p.strip() for p in re.split(r"\n\s*\n", text or "") if p.strip()]
    if not paras:
        return "<p><br /></p>"
    return "".join(f"<p>{_html.escape(p).replace(chr(10), '<br />')}</p>" for p in paras)
