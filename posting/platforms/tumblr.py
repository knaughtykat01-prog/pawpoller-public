"""Tumblr poster — spec 024 (4.60.0).

A Library piece becomes one Tumblr post in Neue Post Format: the image (GIFs are images) or one
video (audio: Tumblr's API refuses uploads, so it isn't offered), then the title in bold and the caption as text blocks (split at Tumblr's
4,096 characters, never cut), the alt text, up to 30 tags. Credit and the "Posted via PawPoller"
line are already in the description (``artwork_reader.build_artwork_package``).

**Ratings.** ``max_rating = "mature"`` — Tumblr bans explicit sex acts outright, so adult work is
refused before anything is sent. Mature work must carry Tumblr's Mature label, and Tumblr's public
docs don't say whether an app can set it (research R3), so a mature post is created as a **draft**
with the label, read back, and published only if the label is on it. Otherwise it stays a draft and
the result asks the owner to finish it on Tumblr (``needs_attention``). Unknown counts as unlabelled.

**Signing in.** The Connect button's OAuth 2 tokens win; the pasted OAuth 1 set still works. A
token within two minutes of expiry is refreshed first, the blog's ownership re-checked, and the new
pair stored on this account (``_save_creds``).
"""
from __future__ import annotations

import logging
import os
import re
import time

import config
from clients.tum.writer import TumError, TumWriter, labelled
from posting.platforms.base import PlatformPoster, PostResult, StoryUploadPackage, rating_rank

logger = logging.getLogger(__name__)

MB = 1024 * 1024
TEXT_BLOCK_MAX = 4096
MAX_TAGS = 30
LABEL_CATEGORIES = ("sexual_themes", "violence", "drug_use")
DEFAULT_CATEGORIES = ["sexual_themes"]
_IMAGE = ["png", "jpg", "jpeg", "gif", "webp"]
_VIDEO = ["mp4", "mov"]
# Audio: Tumblr's public API refuses every uploaded audio file (400, subcode 8005 — tried five block shapes
# and two MP3s live, 2026-10-09), so audio is not offered: an MP3 piece is greyed out with the reason.
_AUDIO: list[str] = []
_MIME = {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg", "gif": "image/gif", "webp": "image/webp",
         "mp4": "video/mp4", "mov": "video/quicktime", "mp3": "audio/mpeg"}

DRAFT_SENTENCE = ("Saved as a draft on Tumblr. Tumblr didn't take the Mature label from PawPoller. "
                  "Open it, add the label and publish.")
PRIVATE_SENTENCE = ("Made private on Tumblr: it's now Mature and Tumblr didn't take the label from PawPoller. "
                    "Open it, add the label and make it public again.")


class TumblrPoster(PlatformPoster):

    platform_id = "tum"
    platform_name = "Tumblr"
    supports_edit = True
    supports_artwork_edit = True
    supports_file_replace = True
    min_post_interval = 5
    accepted_file_types = _IMAGE + _VIDEO + _AUDIO
    accepted_media = {"image": list(_IMAGE), "video": list(_VIDEO), "audio": list(_AUDIO)}
    max_bytes_by_kind = {"image": 20 * MB, "gif": 10 * MB, "video": 500 * MB}
    max_rating = "mature"
    requires_mode = "any"

    # -- signing in ----------------------------------------------------------------------

    async def _writer(self) -> TumWriter:
        creds = self._resolve_creds("tum", config.get_settings())
        blog = creds.get("tum_blog", "")
        if not blog:
            raise TumError("Tumblr isn't set up — add your blog in Settings → Accounts → Tumblr")
        access = creds.get("tum_oauth2_access_token", "")
        if creds.get("tum_oauth2_refresh_token"):
            try:
                expires = int(creds.get("tum_oauth2_expires_at") or 0)
            except ValueError:
                expires = 0
            if not access or expires - time.time() < 120:
                from clients.tum import writer as w
                fresh = await w.refresh(creds["tum_oauth2_refresh_token"], creds.get("tum_api_key", ""),
                                        creds.get("tum_consumer_secret", ""))
                async with TumWriter(blog, access_token=fresh["tum_oauth2_access_token"]) as probe:
                    who = await probe.user_info()
                if not owns(who, blog):
                    raise TumError(f"Tumblr sign-in is for {who['name'] or 'another user'}, who doesn't own {blog} "
                                   "— press Connect again as the blog's owner")
                fresh["tum_oauth2_user"] = who["name"]
                self._save_creds("tum", fresh)     # the old refresh token may be spent: store the new one now
                access = fresh["tum_oauth2_access_token"]
        return TumWriter(blog, access_token=access, consumer_key=creds.get("tum_api_key", ""),
                         consumer_secret=creds.get("tum_consumer_secret", ""),
                         oauth_token=creds.get("tum_oauth_token", ""),
                         oauth_token_secret=creds.get("tum_oauth_token_secret", ""))

    # -- posting -------------------------------------------------------------------------

    async def post(self, package: StoryUploadPackage) -> PostResult:
        _t = self._start_timer()
        try:
            content, files = media_content(package)
            content += npf_text_blocks(package.title or "", package.description or "")
            mature = rating_rank(package.rating) == 1
            cats = label_categories(package)
            async with await self._writer() as tw:
                r = await tw.create_post(content, tags=fit_tags(package.tags), files=files,
                                         state="draft" if mature else "published", label=mature, categories=cats)
                if not r["id"]:
                    return PostResult(success=False, error="Tumblr didn't return a post id",
                                      duration_seconds=self._elapsed(_t))
                note = ""
                if r.get("state") == "transcoding":
                    r = await self._settled(tw, r, package.title or "")
                    note = ("Tumblr is still processing the video" if r["id"] else
                            "Tumblr is still processing the video — it will appear on your blog shortly")
                    if not r["id"]:
                        return PostResult(success=True, external_url=r["url"], error=note,
                                          duration_seconds=self._elapsed(_t))
                if mature:
                    return await self._publish_if_labelled(tw, r, cats, _t)
            return PostResult(success=True, external_id=r["id"], external_url=r["url"],
                              error=note or None, duration_seconds=self._elapsed(_t))
        except TumError as e:
            return PostResult(success=False, error=str(e), duration_seconds=self._elapsed(_t))
        except Exception as e:
            logger.error("Tumblr post failed: %s", e, exc_info=True)
            return PostResult(success=False, error=str(e), duration_seconds=self._elapsed(_t))

    SETTLE_TRIES, SETTLE_WAIT = 6, 10.0

    async def _settled(self, tw: TumWriter, r: dict, title: str) -> dict:
        """A video still transcoding is answered with a TEMPORARY id; the finished post gets another one
        (live, 2026-10-09: the id from the create call stayed 404). Watch the blog's newest posts for about a
        minute and take the video post that starts with our title. → {"id", "url", "state"}; id "" when it
        hasn't appeared yet (url is then the blog itself)."""
        import asyncio
        want = (title or "").strip()
        since = int(time.time()) - 300          # an older video with the same title is not this one
        for i in range(self.SETTLE_TRIES):
            if i:
                await asyncio.sleep(self.SETTLE_WAIT)
            try:
                posts = await tw.recent_posts(5)
            except TumError:
                continue
            for p in posts:
                content = p.get("content") or []
                texts = [b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text"]
                if int(p.get("timestamp") or 0) < since:
                    continue
                if (any(isinstance(b, dict) and b.get("type") == "video" for b in content)
                        and (not want or (texts and texts[0].strip() == want))):
                    pid = str(p.get("id_string") or p.get("id") or "")
                    if pid and pid != r["id"]:
                        return {"id": pid, "url": tw.post_url(pid), "state": str(p.get("state") or "")}
        host = tw.blog if "." in tw.blog else f"{tw.blog}.tumblr.com"
        return {"id": "", "url": f"https://{host}/", "state": "transcoding"}

    async def _publish_if_labelled(self, tw: TumWriter, r: dict, cats: list[str], _t: float) -> PostResult:
        """Draft → read back → publish only with the label on (FR-003). Any doubt leaves the draft."""
        try:
            ok = labelled(await tw.get_post(r["id"]))
        except Exception as e:                       # unknown counts as unlabelled
            logger.warning("Tumblr: couldn't read back draft %s: %s", r["id"], e)
            ok = False
        if ok:
            try:
                current = await tw.get_post(r["id"])
                await tw.edit_post(r["id"], current.get("content") or [], state="published", label=True,
                                   categories=cats, tags=current.get("tags"))
                return PostResult(success=True, external_id=r["id"], external_url=r["url"],
                                  duration_seconds=self._elapsed(_t))
            except Exception as e:
                logger.warning("Tumblr: publishing labelled draft %s failed: %s", r["id"], e)
        return PostResult(success=True, external_id=r["id"], external_url=r["url"],
                          needs_attention=DRAFT_SENTENCE, duration_seconds=self._elapsed(_t))

    async def edit(self, external_id: str, package: StoryUploadPackage) -> PostResult:
        """Edit in place: keep the media blocks exactly as Tumblr returned them (no re-upload), rebuild
        text, tags and label. A post that is now Mature gets the label; if it doesn't stick the post is
        made private (never left public unlabelled)."""
        _t = self._start_timer()
        try:
            async with await self._writer() as tw:
                try:
                    current = await tw.get_post(external_id)
                except TumError as e:
                    return PostResult(success=False, error=f"Can't edit this post on Tumblr: {e}",
                                      duration_seconds=self._elapsed(_t))
                old = current.get("content")
                if not isinstance(old, list):
                    return PostResult(success=False, error="Can't edit this post on Tumblr: it was made in Tumblr's "
                                      "old format, which apps can't edit", duration_seconds=self._elapsed(_t))
                media = [b for b in old if isinstance(b, dict) and b.get("type") in ("image", "video", "audio")]
                content = media + npf_text_blocks(package.title or "", package.description or "")
                mature = rating_rank(package.rating) == 1
                cats = label_categories(package)
                r = await tw.edit_post(external_id, content, tags=fit_tags(package.tags), label=mature,
                                       categories=cats)
                url = r.get("url") or tw.post_url(external_id)
                if mature:
                    try:
                        ok = labelled(await tw.get_post(external_id))
                    except Exception:
                        ok = False
                    if not ok:
                        await tw.edit_post(external_id, content, tags=fit_tags(package.tags), state="private",
                                           label=True, categories=cats)
                        return PostResult(success=True, external_id=external_id, external_url=url,
                                          needs_attention=PRIVATE_SENTENCE, duration_seconds=self._elapsed(_t))
            return PostResult(success=True, external_id=external_id, external_url=url,
                              duration_seconds=self._elapsed(_t))
        except TumError as e:
            return PostResult(success=False, error=str(e), duration_seconds=self._elapsed(_t))
        except Exception as e:
            logger.error("Tumblr edit failed: %s", e, exc_info=True)
            return PostResult(success=False, error=str(e), duration_seconds=self._elapsed(_t))

    async def replace_file(self, external_id: str, file_path: str) -> PostResult:
        """Upload the new file and put it where the old media block was; text and tags stay."""
        _t = self._start_timer()
        try:
            ext = os.path.splitext(file_path)[1].lstrip(".").lower()
            async with await self._writer() as tw:
                current = await tw.get_post(external_id)
                old = current.get("content") or []
                new_block = _media_block(_kind_of_ext(ext), ext, "f0", "")
                swapped, done = [], False
                for b in old:
                    if not done and isinstance(b, dict) and b.get("type") in ("image", "video", "audio"):
                        if b.get("alt_text"):
                            new_block["alt_text"] = b["alt_text"]
                        swapped.append(new_block)
                        done = True
                    else:
                        swapped.append(b)
                if not done:
                    swapped.insert(0, new_block)
                r = await tw.edit_post(external_id, swapped, files={"f0": file_path})
            return PostResult(success=True, external_id=external_id, external_url=r.get("url", ""),
                              duration_seconds=self._elapsed(_t))
        except TumError as e:
            return PostResult(success=False, error=str(e), duration_seconds=self._elapsed(_t))
        except Exception as e:
            logger.error("Tumblr file replace failed: %s", e, exc_info=True)
            return PostResult(success=False, error=str(e), duration_seconds=self._elapsed(_t))

    def validate(self, package: StoryUploadPackage) -> list[str]:
        errors: list[str] = []
        refusal = self.refusal(package)
        if refusal:
            errors.append(refusal)
        if not package.media_kind:
            errors.append("Tumblr takes pictures, video and audio from PawPoller — stories aren't posted to Tumblr yet")
        path = package.file_path or ""
        if not path:
            errors.append("Tumblr needs a file to post")
        elif not os.path.isfile(path):
            errors.append(f"File not found: {path}")
        else:
            over = size_refusal(self.max_bytes_by_kind, package.media_kind, package.file_type, os.path.getsize(path),
                                self.platform_name)
            if over:
                errors.append(over)
        creds = self._resolve_creds("tum", config.get_settings())
        if not creds.get("tum_blog"):
            errors.append("Tumblr isn't set up — add your blog in Settings → Accounts → Tumblr")
        elif not (creds.get("tum_oauth2_refresh_token") or creds.get("tum_oauth2_access_token")
                  or (creds.get("tum_consumer_secret") and creds.get("tum_oauth_token")
                      and creds.get("tum_oauth_token_secret"))):
            errors.append("Tumblr isn't connected for posting — press Connect (Settings → Accounts → Tumblr)")
        return errors


# -- helpers (module level so the Posts page and tests share them) ------------------------

def owns(who: dict, blog: str) -> bool:
    from clients.tum.client import _normalise_blog
    want = _normalise_blog(blog).split(".")[0].lower()
    return want in {b.lower() for b in who.get("blogs") or []}


def fit_tags(tags) -> list[str]:
    out, seen = [], set()
    for t in tags or []:
        t = str(t).replace("_", " ").replace(",", " ").strip().lstrip("#")
        if t and t.lower() not in seen:
            seen.add(t.lower())
            out.append(t)
        if len(out) >= MAX_TAGS:
            break
    return out


def label_categories(package: StoryUploadPackage) -> list[str]:
    raw = (package.extra or {}).get("tum_label_categories")
    if raw is None:
        return list(DEFAULT_CATEGORIES)
    if isinstance(raw, str):
        raw = [x.strip() for x in raw.split(",")]
    return [c for c in raw if c in LABEL_CATEGORIES]


def size_refusal(limits: dict, kind: str, ext: str, size: int, site: str = "Tumblr") -> str | None:
    key = "gif" if (ext or "").lower() == "gif" and "gif" in limits else (kind or "")
    cap = limits.get(key)
    if cap and size > cap:
        what = "GIFs" if key == "gif" else f"{key} files"
        return f"{site} takes {what} up to {cap // MB} MB — this one is {size / MB:.1f} MB"
    return None


def _kind_of_ext(ext: str) -> str:
    return "video" if ext in _VIDEO else "audio" if ext in _AUDIO else "image"


def _media_block(kind: str, ext: str, ident: str, alt: str, title: str = "") -> dict:
    media = {"type": _MIME.get(ext, "application/octet-stream"), "identifier": ident}
    if kind == "video":
        return {"type": "video", "media": media}
    if kind == "audio":
        b = {"type": "audio", "provider": "tumblr", "media": media}
        if title:
            b["title"] = title
        return b
    b = {"type": "image", "media": [media]}
    if alt:
        b["alt_text"] = alt[:4096]
    return b


def media_content(package: StoryUploadPackage) -> tuple[list[dict], dict[str, str]]:
    """The media block(s) for one piece and the files to upload with them."""
    path = package.file_path or ""
    if not path:
        return [], {}
    ext = (package.file_type or os.path.splitext(path)[1].lstrip(".")).lower()
    kind = package.media_kind or _kind_of_ext(ext)
    alt = str((package.extra or {}).get("alt_text") or "")
    blocks = [_media_block(kind, ext, "f0", alt, package.title or "")]
    files = {"f0": path}
    if kind == "audio" and package.thumbnail_path and os.path.isfile(package.thumbnail_path):
        cext = os.path.splitext(package.thumbnail_path)[1].lstrip(".").lower()
        blocks[0]["poster"] = [{"type": _MIME.get(cext, "image/jpeg"), "identifier": "f1"}]
        files["f1"] = package.thumbnail_path
    return blocks, files


# -- caption → NPF text blocks ---------------------------------------------------------------

_INLINE = re.compile(r"\[([^\]\n]+)\]\((https?://[^)\s]+)\)|\*\*([^*\n]+)\*\*|(?<![*\w])\*([^*\n]+)\*(?!\*)"
                     r"|(https?://[^\s<>()]+)")


def _inline(md: str) -> tuple[str, list[dict]]:
    """Plain text + NPF formatting ranges for links, **bold**, *italic* and bare URLs."""
    out, fmt, pos = [], [], 0
    for m in _INLINE.finditer(md):
        out.append(md[pos:m.start()])
        start = sum(len(s) for s in out)
        if m.group(1) is not None:
            text, f = m.group(1), {"type": "link", "url": m.group(2)}
        elif m.group(3) is not None:
            text, f = m.group(3), {"type": "bold"}
        elif m.group(4) is not None:
            text, f = m.group(4), {"type": "italic"}
        else:
            url = m.group(5).rstrip(".,;:!?")
            text, f = url, {"type": "link", "url": url}
            tail = m.group(5)[len(url):]
            out.append(text)
            fmt.append({"start": start, "end": start + len(text), **f})
            out.append(tail)
            pos = m.end()
            continue
        out.append(text)
        fmt.append({"start": start, "end": start + len(text), **f})
        pos = m.end()
    out.append(md[pos:])
    return "".join(out), fmt


def _split_long(text: str, limit: int = TEXT_BLOCK_MAX) -> list[str]:
    """Split on sentence ends, then spaces — never mid-word unless a single word is longer than a block."""
    if len(text) <= limit:
        return [text]
    parts, rest = [], text
    while len(rest) > limit:
        cut = max(rest.rfind(". ", 0, limit), rest.rfind("! ", 0, limit), rest.rfind("? ", 0, limit))
        cut = cut + 1 if cut > limit // 2 else rest.rfind(" ", 0, limit)
        if cut <= 0:
            cut = limit
        parts.append(rest[:cut].rstrip())
        rest = rest[cut:].lstrip()
    if rest:
        parts.append(rest)
    return parts


def npf_text_blocks(title: str, markdown: str) -> list[dict]:
    """Bold title first, then one block per paragraph (split further when over 4,096 characters)."""
    blocks: list[dict] = []
    title = (title or "").strip()
    if title:
        blocks.append({"type": "text", "text": title[:TEXT_BLOCK_MAX],
                       "formatting": [{"start": 0, "end": min(len(title), TEXT_BLOCK_MAX), "type": "bold"}]})
    for para in re.split(r"\n\s*\n", (markdown or "").replace("\r\n", "\n")):
        para = para.strip()
        if not para:
            continue
        for chunk in _split_long(para):
            text, fmt = _inline(chunk)
            block: dict = {"type": "text", "text": text}
            if fmt:
                block["formatting"] = fmt
            blocks.append(block)
    return blocks
