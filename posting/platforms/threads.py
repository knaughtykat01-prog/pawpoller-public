"""Threads work poster (spec 030, 4.61.0).

Posts a Library piece to Threads: a picture as a picture post, a video as a video post, and a GIF as a looping
video (Threads freezes a GIF sent as a picture; ``posting.video_convert`` re-encodes it with the bundled ffmpeg).
Threads, like Instagram, never takes file bytes: it fetches each file from a public URL, so the file climbs the
Instagram image-host ladder (``posting.ig_host``) and is cleaned up after.

Caption = the description (or the title), fitted to Threads' 500 characters at a word boundary with the credit
lines kept; the first tag is the post's one Threads topic. **Rating:** ``max_rating = "general"`` — Threads uses
Instagram's Community Guidelines. Post-only: Threads has no edit API.
"""
from __future__ import annotations

import logging
import os
import tempfile

import config
from posting.platforms.base import PlatformPoster, PostResult, StoryUploadPackage

logger = logging.getLogger(__name__)

MB = 1024 * 1024
TEXT_LIMIT = 500
MIN_WIDTH = 320
MAX_RATIO = 10.0
VIDEO_MAX_SECONDS = 300
_VIDEO = ("mp4", "mov")


class ThreadsPoster(PlatformPoster):

    platform_id = "thr"
    platform_name = "Threads"
    supports_edit = False
    supports_file_replace = False
    min_post_interval = 5
    max_file_size = 0                        # pictures are resized to fit, never refused for size
    accepted_file_types = ["jpg", "jpeg", "png", "webp", "gif", "mp4", "mov"]
    max_bytes_by_kind = {"video": 1024 * MB}
    max_rating = "general"
    requires_mode = "any"                    # the image-host ladder finds Meta a URL on either side

    async def post(self, package: StoryUploadPackage) -> PostResult:
        _t = self._start_timer()
        temps: list[str] = []
        hosted = None
        try:
            creds = self._resolve_creds("thr", config.get_settings())
            token = creds.get("thr_access_token", "")
            if not token:
                return PostResult(success=False, error="Threads account isn't connected",
                                  duration_seconds=self._elapsed(_t))
            from clients.thr.client import ThrClient, ThrError
            from posting import activity, ig_host
            path, kind, temps = await prepare_media(package.file_path or "", _kind(package))
            activity.step("thr", "Uploading", "Putting the file where Threads can fetch it")
            try:
                hosted = await ig_host.host_images([path], config.get_settings())
            except ig_host.NoPublicHost as e:
                return PostResult(success=False, error=str(e), duration_seconds=self._elapsed(_t))
            text, shortened = fit_caption(package.description or package.title or "")
            item = {"kind": kind, "url": hosted.urls[0], "alt": str((package.extra or {}).get("alt_text") or "")}
            activity.step("thr", "Publishing", "Threads is processing the video" if kind == "video" else None)
            client = ThrClient(access_token=token, user_id=creds.get("thr_user_id", ""))
            try:
                r = await client.create_media_post(text, [item], topic_of(package.tags))
            except ThrError as e:
                return PostResult(success=False, error=str(e), duration_seconds=self._elapsed(_t))
            finally:
                await client.close()
            return PostResult(success=True, external_id=r["id"], external_url=r.get("url", ""),
                              error=SHORTENED if shortened else None, duration_seconds=self._elapsed(_t))
        except Exception as e:
            logger.error("Threads post failed: %s", e, exc_info=True)
            return PostResult(success=False, error=str(e), duration_seconds=self._elapsed(_t))
        finally:
            if hosted:
                await hosted.close()
            for t in temps:
                _remove(t)

    async def edit(self, external_id: str, package: StoryUploadPackage) -> PostResult:
        return PostResult(success=False, error="Threads doesn't let apps edit a post")

    async def replace_file(self, external_id: str, file_path: str) -> PostResult:
        return PostResult(success=False, error="Threads doesn't let apps replace a post's file")

    def validate(self, package: StoryUploadPackage) -> list[str]:
        errors: list[str] = []
        path = package.file_path or ""
        kind = _kind(package)
        if not path:
            errors.append("Threads needs a picture, GIF or video")
        elif not os.path.isfile(path):
            errors.append(f"File not found: {path}")
        elif kind == "video":
            if os.path.getsize(path) > self.max_bytes_by_kind["video"]:
                errors.append("Video is over 1 GB — Threads takes up to 1 GB")
            if package.duration_s and package.duration_s > VIDEO_MAX_SECONDS:
                errors.append(f"Video runs {package.duration_s / 60:.1f} min — Threads takes up to 5 minutes")
        else:
            w, h = package.width, package.height
            if not (w and h):
                try:
                    from PIL import Image
                    with Image.open(path) as im:
                        w, h = im.size
                except Exception:
                    w = h = 0
            if w and h and max(w / h, h / w) > MAX_RATIO:
                errors.append("Threads doesn't take pictures more than 10 times wider than tall (or taller than wide)")
            if _is_gif(path):
                from posting import video_convert
                if not video_convert.ffmpeg_available():
                    errors.append(video_convert.NO_ENCODER)
        from posting import ig_host
        if not ig_host.first_available_rung(config.get_settings()):
            errors.append("Threads needs a public address to fetch the file from, and none is available: turn on "
                          "the PawPoller relay or the temporary tunnel in Settings → Posting → Instagram image "
                          "host, pair this app with your server, or set IG_PUBLIC_BASE_URL on a server")
        return errors


SHORTENED = "The caption was shortened to fit Threads' 500 characters"


def _kind(package: StoryUploadPackage) -> str:
    ext = (package.file_type or os.path.splitext(package.file_path or "")[1].lstrip(".")).lower()
    return "video" if ext in _VIDEO else "image"


def _is_gif(path: str) -> bool:
    return path.lower().endswith(".gif")


def _remove(path: str) -> None:
    try:
        os.remove(path)
    except OSError:
        pass


async def prepare_media(path: str, kind: str) -> tuple[str, str, list[str]]:
    """A file Threads will take → (path, kind, temp files to delete). A GIF becomes an MP4 (kind video); a
    picture narrower than 320 px is enlarged (ig_media only ever shrinks)."""
    if kind == "image" and _is_gif(path):
        from posting import video_convert
        out = await video_convert.gif_to_mp4(path)
        return out, "video", [out]
    if kind == "image":
        from PIL import Image
        with Image.open(path) as im:
            if im.width < MIN_WIDTH:
                scale = MIN_WIDTH / im.width
                big = im.convert("RGBA").resize((MIN_WIDTH, max(1, round(im.height * scale))), Image.LANCZOS)
                fd, out = tempfile.mkstemp(suffix=".png", prefix="pp_thr_")
                os.close(fd)
                big.save(out, "PNG")
                return out, "image", [out]
    return path, kind, []


def fit_caption(text: str, limit: int = TEXT_LIMIT) -> tuple[str, bool]:
    """Fit *text* to *limit* characters → (text, shortened). The body is cut at a word boundary with "…"; the
    trailing short paragraphs (the artist credit and the "Posted via PawPoller" line) are kept whole.
    ponytail: "trailing short paragraphs" (≤ 200 chars, at most 2) stands in for knowing which lines the credit
    added; a very short last paragraph of the owner's own text is kept whole too, which is harmless."""
    text = (text or "").strip()
    if len(text) <= limit:
        return text, False
    paras = text.split("\n\n")
    tail: list[str] = []
    while len(paras) > 1 and len(tail) < 2 and len(paras[-1]) <= 200:
        tail.insert(0, paras.pop())
    tail_text = ("\n\n" + "\n\n".join(tail)) if tail else ""
    room = limit - len(tail_text) - 1                   # 1 for the "…"
    body = "\n\n".join(paras)
    if room < 20:                                        # no room for the body: cut the whole thing instead
        body, tail_text, room = text, "", limit - 1
    cut = body[:room]
    if " " in cut[room // 2:]:
        cut = cut[:cut.rstrip().rfind(" ")]
    return cut.rstrip() + "…" + tail_text, True


def topic_of(tags) -> str:
    """The first usable tag as Threads' one topic: no full stops or ampersands, 1–50 characters."""
    for t in tags or []:
        topic = " ".join(str(t).replace("_", " ").replace(".", "").replace("&", "").split())[:50].strip()
        if topic:
            return topic
    return ""
