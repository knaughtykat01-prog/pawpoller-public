"""Facebook Page poster (spec 022, 4.57.0).

Posts a Library piece to a Facebook **Page**: an image or GIF as a photo post, a video as
a Page video. The Pages API takes the file itself, so (unlike Instagram) nothing has to be
hosted publicly and the desktop app posts exactly as a server does.

Caption = the description (or title) with the tags as hashtags below it, the same shape as
Instagram's. **Rating:** ``max_rating = "general"`` — Facebook's Community Standards forbid
sexual content and most nudity, so anything above general is refused before the network.
Post-only in phase 1 (no caption edit, no file replace). A GIF goes up as a video so it
keeps moving (as a photo Facebook keeps only a still).
"""
from __future__ import annotations

import logging
import os
import tempfile

import config
from posting.platforms.base import PlatformPoster, PostResult, StoryUploadPackage

logger = logging.getLogger(__name__)

PHOTO_MAX = 10 * 1024 * 1024          # Facebook's photo cap
VIDEO_MAX = 1024 * 1024 * 1024        # simple (non-resumable) upload ceiling we allow
_MAX_HASHTAGS = 30
_PHOTO_TYPES = ["jpg", "jpeg", "png", "gif", "webp"]
_VIDEO_TYPES = ["mp4", "mov"]


class FacebookPoster(PlatformPoster):

    platform_id = "fb"
    platform_name = "Facebook"
    supports_edit = False
    supports_artwork_edit = False
    supports_file_replace = False
    min_post_interval = 10
    max_file_size = 0                  # photos are shrunk to fit below; video checked in validate()
    accepted_file_types = _PHOTO_TYPES + _VIDEO_TYPES
    accepted_media = {"image": list(_PHOTO_TYPES), "video": list(_VIDEO_TYPES), "audio": []}
    max_rating = "general"
    requires_mode = "any"

    async def post(self, package: StoryUploadPackage) -> PostResult:
        _t = self._start_timer()
        temp = None
        try:
            creds = self._resolve_creds("fb", config.get_settings())
            token, page = creds.get("fb_page_token", ""), creds.get("fb_page_id", "")
            if not (token and page):
                return PostResult(success=False, error="Facebook isn't connected (Settings → Platforms → Facebook)",
                                  duration_seconds=self._elapsed(_t))
            from clients.fb.client import FbClient, FbError
            from posting import activity
            caption = build_caption(package)
            async with FbClient(page_token=token, page_id=page) as client:
                try:
                    if _is_video(package):
                        activity.step("fb", "Uploading", "Sending the video to Facebook")
                        r = await client.post_video(package.file_path or "", package.title or "", caption)
                    else:
                        path, temp = fit_photo(package.file_path or "")
                        activity.step("fb", "Uploading", None)
                        r = await client.post_photos([path], caption)
                except FbError as e:
                    return PostResult(success=False, error=str(e), duration_seconds=self._elapsed(_t))
            if r.get("id"):
                return PostResult(success=True, external_id=r["id"], external_url=r.get("url", ""),
                                  duration_seconds=self._elapsed(_t))
            return PostResult(success=False, error="Facebook didn't return a post id",
                              duration_seconds=self._elapsed(_t))
        except Exception as e:
            logger.error("Facebook post failed: %s", e, exc_info=True)
            return PostResult(success=False, error=str(e), duration_seconds=self._elapsed(_t))
        finally:
            if temp:
                try:
                    os.remove(temp)
                except OSError:
                    pass

    async def edit(self, external_id: str, package: StoryUploadPackage) -> PostResult:
        return PostResult(success=False, error="Editing Facebook posts isn't supported yet")

    async def replace_file(self, external_id: str, file_path: str) -> PostResult:
        return PostResult(success=False, error="Facebook doesn't allow replacing a posted file")

    def validate(self, package: StoryUploadPackage) -> list[str]:
        errors: list[str] = []
        refusal = self.refusal(package)
        if refusal:
            errors.append(refusal)
        path = package.file_path or ""
        if not path:
            errors.append("Facebook needs an image or video file")
        elif not os.path.isfile(path):
            errors.append(f"File not found: {path}")
        elif _is_video(package):
            if os.path.getsize(path) > VIDEO_MAX:
                errors.append("Video is over 1 GB — too big to send to Facebook in one go")
        return errors


def _is_video(package: StoryUploadPackage) -> bool:
    # A GIF goes up as a video: sent to /photos Facebook keeps one still frame (live proof
    # 2026-10-09, T021); /videos takes the .gif as-is and it plays as a short looping clip.
    return bool(package.file_path) and (package.file_type or "").lower() in (*_VIDEO_TYPES, "gif")


def fit_photo(path: str) -> tuple[str, str | None]:
    """A file Facebook will take: GIF/JPEG/PNG under 10 MB pass through; WebP, or a still over
    10 MB, becomes a JPEG (long edge ≤ 4096). Returns (path_to_send, temp_to_delete_or_None)."""
    ext = os.path.splitext(path)[1].lower().lstrip(".")
    if ext == "gif" or (ext in ("jpg", "jpeg", "png") and os.path.getsize(path) <= PHOTO_MAX):
        return path, None
    from PIL import Image
    with Image.open(path) as im:
        im = im.convert("RGB")
        im.thumbnail((4096, 4096))
        fd, out = tempfile.mkstemp(suffix=".jpg", prefix="pp_fb_")
        os.close(fd)
        for q in (90, 82, 72):
            im.save(out, "JPEG", quality=q, optimize=True)
            if os.path.getsize(out) <= PHOTO_MAX:
                break
    return out, out


def build_caption(package: StoryUploadPackage) -> str:
    parts = []
    body = (package.description or package.title or "").strip()
    if body:
        parts.append(body)
    seen, tags = set(), []
    for t in package.tags or []:
        h = "".join(ch for ch in t if ch.isalnum() or ch == "_")
        if h and h.lower() not in seen:
            seen.add(h.lower())
            tags.append("#" + h)
        if len(tags) >= _MAX_HASHTAGS:
            break
    if tags:
        parts.append(" ".join(tags))
    return "\n\n".join(parts)
