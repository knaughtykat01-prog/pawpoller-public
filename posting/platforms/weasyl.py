"""Weasyl platform poster.

Uses the existing WeasylClient (clients/weasyl/client.py) with API key auth.
The API key is sent on every request as X-Weasyl-API-Key header.

Post flow:
  1. POST /submit/literary — multipart with file + metadata

Edit flow (4.54.4):
  1. GET /edit/submission?submitid={id} — current values; POST /edit/submission
  2. POST /submit/tags — tags are their own form
  3. POST /reupload/submission — the file; /reupload/cover + /manage/thumbnail — the cover

Rating mapping:
  General → 10, Mature → 30, Adult → 40
"""

from __future__ import annotations

import contextlib
import logging
import os
import re
import tempfile

import config
from posting.platforms.base import PlatformPoster, PostResult, StoryUploadPackage
from clients.weasyl.client import WeasylClient

logger = logging.getLogger(__name__)


class WeasylPoster(PlatformPoster):

    platform_id = "ws"
    platform_name = "Weasyl"
    supports_edit = True
    supports_file_replace = True   # Weasyl's Reupload forms (4.54.4)
    min_post_interval = 5
    max_file_size = 10 * 1024 * 1024  # 10 MB for text
    # mp3 (MEDIATYPES phase 2, 4.19.3): Weasyl's multimedia submission, ≤ 15 MB, the
    # poster as cover + thumbnail. Weasyl takes no video files (embeds only).
    max_audio_size = 15 * 1024 * 1024
    accepted_file_types = ["pdf", "txt", "md", "png", "jpg", "jpeg", "gif", "webp", "mp3"]

    def __init__(self):
        self._client: WeasylClient | None = None

    async def _ensure_client(self) -> WeasylClient:
        if self._client:
            return self._client
        settings = config.get_settings()
        creds = self._resolve_creds("ws", settings)
        api_key = creds.get("ws_api_key", "")
        if not api_key:
            raise RuntimeError("Weasyl API key not configured")
        self._client = WeasylClient(api_key=api_key)
        return self._client

    async def post(self, package: StoryUploadPackage) -> PostResult:
        _t = self._start_timer()
        try:
            client = await self._ensure_client()
            if not package.file_path:
                return PostResult(success=False, error="No file for Weasyl upload", duration_seconds=self._elapsed(_t))

            rating = _rating_to_ws(package.rating)
            tags_str = " ".join(package.tags)

            is_image = package.file_type in ("png", "jpg", "jpeg", "gif", "webp")
            if _is_audio(package):
                # 4.19.3: a multimedia submission — the piece's own subtype wins,
                # else 3010 Original Music.
                try:
                    subtype = int(package.extra.get("subtype") or 3010)
                except (TypeError, ValueError):
                    subtype = 3010
                result = await client.submit_multimedia(
                    package.file_path,
                    title=package.title,
                    description=package.description,
                    tags=tags_str,
                    rating=rating,
                    subtype=subtype,
                    cover_path=package.thumbnail_path,
                )
            elif is_image:
                settings = config.get_settings()
                try:
                    subtype = int(package.extra.get("subtype")
                                  or settings.get("artwork_ws_subtype") or 0)
                except (TypeError, ValueError):
                    subtype = 0
                result = await client.submit_visual(
                    package.file_path,
                    title=package.title,
                    description=package.description,
                    tags=tags_str,
                    rating=rating,
                    subtype=subtype,
                    thumbnail_path=package.thumbnail_path,
                )
            else:
                with _publishable(package.file_path) as story_file:
                    result = await client.submit_literary(
                        story_file,
                        title=package.title,
                        description=package.description,
                        tags=tags_str,
                        rating=rating,
                        cover_path=package.thumbnail_path,
                    )

            return PostResult(
                success=True,
                external_id=result.get("submission_id", ""),
                external_url=result.get("url", ""),
                duration_seconds=self._elapsed(_t),
            )
        except Exception as e:
            logger.error("WS post failed: %s", e, exc_info=True)
            return PostResult(success=False, error=str(e), duration_seconds=self._elapsed(_t))

    async def edit(self, external_id: str, package: StoryUploadPackage) -> PostResult:
        """Update a Weasyl submission: metadata, then the file, then the cover (4.54.4).

        Weasyl CAN replace all three (its own Reupload links: ``/reupload/submission``,
        ``/reupload/cover``, ``/manage/thumbnail``) — the "delete and re-post" note this
        used to return was never true. A story goes up as Markdown with the markers
        stripped, exactly like a new post; a story's / audio's cover also becomes the
        gallery thumbnail. ``skip_content_refresh`` leaves the file alone.
        """
        _t = self._start_timer()
        try:
            client = await self._ensure_client()
            result = await client.edit_submission(
                external_id,
                title=package.title,
                description=package.description,
                tags=" ".join(package.tags),
                rating=_rating_to_ws(package.rating),
            )
            if package.file_path and not package.extra.get("skip_content_refresh"):
                with _publishable(package.file_path) as f:
                    await client.reupload_file(external_id, f)
            is_image = (package.file_type or "").lower() in ("png", "jpg", "jpeg", "gif", "webp")
            if package.thumbnail_path and os.path.isfile(package.thumbnail_path) and not is_image:
                await client.reupload_cover(external_id, package.thumbnail_path)
            return PostResult(success=True, external_id=external_id, external_url=result.get("url", ""),
                              duration_seconds=self._elapsed(_t))
        except Exception as e:
            logger.error("WS edit failed for %s: %s", external_id, e, exc_info=True)
            return PostResult(success=False, external_id=external_id, error=str(e),
                              duration_seconds=self._elapsed(_t))

    async def replace_file(self, external_id: str, file_path: str) -> PostResult:
        try:
            client = await self._ensure_client()
            with _publishable(file_path) as f:
                await client.reupload_file(external_id, f)
            return PostResult(success=True, external_id=external_id)
        except Exception as e:
            return PostResult(success=False, external_id=external_id, error=str(e))

    def validate(self, package: StoryUploadPackage) -> list[str]:
        errors = super().validate(package)
        if len(package.tags) < 2:
            errors.append(f"Weasyl requires at least 2 tags (got {len(package.tags)})")
        if _is_audio(package) and package.file_path:
            import os
            if os.path.isfile(package.file_path) and os.path.getsize(package.file_path) > self.max_audio_size:
                mb = os.path.getsize(package.file_path) / (1024 * 1024)
                errors.append(f"Audio is {mb:.1f} MB — Weasyl takes audio up to 15 MB")
        return errors


_MARKER = re.compile(r"^\s*<!--.*?-->\s*$")


def strip_markers(text: str) -> str:
    """Drop the archive's whole-line ``<!-- @title -->``-style markers (they steer the
    format converters, not readers) and the blank runs they leave."""
    kept = [line for line in text.splitlines() if not _MARKER.match(line)]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(kept)).strip() + "\n"


@contextlib.contextmanager
def _publishable(path: str):
    """A Markdown story goes up as a temp copy without the markers (same file name — Weasyl
    goes by the extension); anything else as it is."""
    if not path.lower().endswith(".md"):
        yield path
        return
    with tempfile.TemporaryDirectory(prefix="pp_ws_") as tmp:
        out = os.path.join(tmp, os.path.basename(path))
        with open(path, encoding="utf-8") as f, open(out, "w", encoding="utf-8") as g:
            g.write(strip_markers(f.read()))
        yield out


_WS_AUDIO_TYPES = ("mp3",)


def _is_audio(package: StoryUploadPackage) -> bool:
    """A package Weasyl files as multimedia (4.19.3): the Library's media_kind or the extension."""
    return package.media_kind == "audio" or (package.file_type or "").lower() in _WS_AUDIO_TYPES


def _rating_to_ws(rating: str) -> int:
    r = rating.lower()
    if r in ("adult", "explicit", "nsfw"):
        return 40
    elif r in ("mature", "questionable"):
        return 30
    return 10
