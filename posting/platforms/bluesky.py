"""Bluesky platform poster.

Uses the existing BskyClient (clients/bsky/client.py) with the new
create_post(), upload_blob(), and delete_post() methods.

Bluesky is used for announcement posts (not full story uploads). Posts are
limited to 300 graphemes and can include up to 4 images.

Post flow:
  1. ensure_logged_in()
  2. (optional) upload_blob(cover_image)
  3. create_post(text, embed=image, labels=nsfw)

Edit flow:
  Bluesky does not support in-place editing. The only option is delete + repost,
  which loses engagement. For announcements this is acceptable.
"""

from __future__ import annotations

import logging
import os

import config
from clients.bsky.client import BskyClient
from posting.platforms.base import PlatformPoster, PostResult, StoryUploadPackage

logger = logging.getLogger(__name__)


class BlueskyPoster(PlatformPoster):

    platform_id = "bsky"
    platform_name = "Bluesky"
    supports_edit = False        # Delete+repost only
    supports_file_replace = False
    min_post_interval = 3
    max_file_size = 1 * 1024 * 1024  # 1 MB for images
    accepted_file_types = ["png", "jpg", "jpeg", "gif"]

    def __init__(self):
        self._client: BskyClient | None = None

    async def _ensure_client(self) -> BskyClient:
        """Get or create an authenticated Bluesky client."""
        if self._client and self._client._logged_in:
            return self._client

        settings = config.get_settings()
        creds = self._resolve_creds("bsky", settings)
        identifier = creds.get("bsky_identifier", "")
        app_password = creds.get("bsky_app_password", "")
        if not identifier or not app_password:
            raise RuntimeError("Bluesky credentials not configured")

        self._client = BskyClient(identifier=identifier, app_password=app_password)
        if not await self._client.ensure_logged_in():
            raise RuntimeError("Bluesky login failed")
        return self._client

    async def post(self, package: StoryUploadPackage) -> PostResult:
        """Create a Bluesky announcement post for a story."""
        _t = self._start_timer()
        try:
            client = await self._ensure_client()

            # Build post text — announcement style
            text = package.description
            if len(text) > 295:
                text = text[:292] + "..."

            # Determine NSFW labels
            labels = None
            if package.rating.lower() in ("adult", "explicit", "nsfw"):
                labels = ["sexual"]
            elif package.rating.lower() in ("mature", "questionable"):
                labels = ["nudity"]

            # Pick the image to embed: an artwork post uses the primary image
            # (the art itself); a story announcement uses the cover thumbnail.
            is_image_post = bool(
                package.file_path
                and package.file_type in ("png", "jpg", "jpeg", "gif", "webp"))
            source_image = package.file_path if is_image_post else package.thumbnail_path

            # Bluesky's blob cap is ~1 MB; downscale/re-encode if needed.
            image_path, tmp_image = (None, None)
            if source_image:
                image_path, tmp_image = _prepare_bsky_image(source_image)

            try:
                result = await client.create_post(
                    text=text,
                    image_path=image_path,
                    # Dedicated alt text when the artwork carries one (gap G6);
                    # the title stays the fallback so alt never regresses to "".
                    image_alt=package.extra.get("alt_text") or package.title,
                    labels=labels,
                )
            finally:
                if tmp_image:
                    try:
                        os.remove(tmp_image)
                    except OSError:
                        pass

            if result and "uri" in result:
                return PostResult(
                    success=True,
                    external_id=result["uri"],
                    external_url=result.get("url", ""),
                    duration_seconds=self._elapsed(_t),
                )

            return PostResult(
                success=False,
                error="Post creation returned no URI",
                duration_seconds=self._elapsed(_t),
            )

        except Exception as e:
            logger.error("BSKY post failed: %s", e, exc_info=True)
            return PostResult(
                success=False,
                error=str(e),
                duration_seconds=self._elapsed(_t),
            )

    async def edit(self, external_id: str, package: StoryUploadPackage) -> PostResult:
        """Bluesky doesn't support editing — delete and repost."""
        _t = self._start_timer()
        try:
            client = await self._ensure_client()

            # Delete old post
            await client.delete_post(external_id)

            # Create new post
            result = await self.post(package)
            result.duration_seconds = self._elapsed(_t)
            return result

        except Exception as e:
            logger.error("BSKY edit (delete+repost) failed: %s", e, exc_info=True)
            return PostResult(success=False, error=str(e), duration_seconds=self._elapsed(_t))

    async def replace_file(self, external_id: str, file_path: str) -> PostResult:
        """Not supported — Bluesky posts don't have replaceable files."""
        return PostResult(success=False, error="Bluesky does not support file replacement")

    def validate(self, package: StoryUploadPackage) -> list[str]:
        errors = []
        # A post needs text OR an image. Story announcements always carry
        # description text; artwork posts may be image-only (no caption).
        has_image = bool(
            (package.file_path and package.file_type in ("png", "jpg", "jpeg", "gif", "webp"))
            or package.thumbnail_path)
        if not package.description and not has_image:
            errors.append("Bluesky post requires text or an image")
        if len(package.description.encode("utf-8")) > 900:
            errors.append("Bluesky text too long (max ~300 graphemes)")
        return errors


# Under Bluesky's ~976 KB blob cap, with headroom.
_BSKY_BLOB_LIMIT = 950_000


def _prepare_bsky_image(path: str) -> tuple[str, str | None]:
    """Return an image path that fits Bluesky's blob cap.

    If the file is already small enough and a natively-supported type, returns
    ``(path, None)`` unchanged. Otherwise downscales/re-encodes to JPEG in a
    temp file and returns ``(temp_path, temp_path)`` so the caller cleans it up.
    """
    try:
        size = os.path.getsize(path)
    except OSError:
        return path, None
    ext = os.path.splitext(path)[1].lower()
    if size <= _BSKY_BLOB_LIMIT and ext in (".jpg", ".jpeg", ".png", ".gif"):
        return path, None
    try:
        import tempfile
        from PIL import Image

        img = Image.open(path)
        if img.mode in ("RGBA", "LA", "P"):
            img = img.convert("RGBA")
            bg = Image.new("RGB", img.size, (255, 255, 255))
            bg.paste(img, mask=img.split()[-1])
            img = bg
        else:
            img = img.convert("RGB")

        fd, tmp = tempfile.mkstemp(suffix=".jpg", prefix="bsky_")
        os.close(fd)
        quality, max_dim = 90, 2048
        while True:
            work = img
            if max(work.size) > max_dim:
                ratio = max_dim / max(work.size)
                work = work.resize(
                    (max(1, int(work.size[0] * ratio)), max(1, int(work.size[1] * ratio))))
            work.save(tmp, "JPEG", quality=quality, optimize=True)
            if os.path.getsize(tmp) <= _BSKY_BLOB_LIMIT or (quality <= 40 and max_dim <= 1024):
                break
            if quality > 40:
                quality -= 10
            else:
                max_dim = int(max_dim * 0.8)
        return tmp, tmp
    except Exception as e:
        logger.warning("BSKY image downscale failed (%s); using original", e)
        return path, None
