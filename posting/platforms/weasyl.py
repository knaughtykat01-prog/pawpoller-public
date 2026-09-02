"""Weasyl platform poster.

Uses the existing WeasylClient (clients/weasyl/client.py) with API key auth.
The API key is sent on every request as X-Weasyl-API-Key header.

Post flow:
  1. POST /submit/literary — multipart with file + metadata

Edit flow:
  1. GET /edit/submission/{id} — scrape form
  2. POST /edit/submission/{id} — update fields

Rating mapping:
  General → 10, Mature → 30, Adult → 40
"""

from __future__ import annotations

import logging

import config
from posting.platforms.base import PlatformPoster, PostResult, StoryUploadPackage
from clients.weasyl.client import WeasylClient

logger = logging.getLogger(__name__)


class WeasylPoster(PlatformPoster):

    platform_id = "ws"
    platform_name = "Weasyl"
    supports_edit = True
    supports_file_replace = False
    min_post_interval = 5
    max_file_size = 10 * 1024 * 1024  # 10 MB for text
    accepted_file_types = ["pdf", "txt", "md", "png", "jpg", "jpeg", "gif", "webp"]

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
            if is_image:
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
                result = await client.submit_literary(
                    package.file_path,
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
        """Edit metadata only — Weasyl's API does not support file replacement.

        If the local file has drifted from what's on the platform, the user
        must delete the Weasyl submission and re-post. The response's
        ``error`` field carries a note when content may be stale so the UI
        can surface it.
        """
        _t = self._start_timer()
        try:
            client = await self._ensure_client()
            rating = _rating_to_ws(package.rating)
            tags_str = " ".join(package.tags)

            result = await client.edit_submission(
                external_id,
                title=package.title,
                description=package.description,
                tags=tags_str,
                rating=rating,
            )

            # Soft warning: content refresh not possible on WS
            warning = None
            if package.file_path:
                logger.info(
                    "WS edit: metadata updated for %s, but file content cannot "
                    "be replaced via API. Delete + repost if content has drifted.",
                    external_id,
                )
                warning = "Metadata updated. Weasyl cannot replace file content — delete + repost if drifted."

            return PostResult(
                success=True,
                external_id=external_id,
                external_url=result.get("url", ""),
                error=warning,  # populated as a non-fatal note
                duration_seconds=self._elapsed(_t),
            )
        except Exception as e:
            logger.error("WS edit failed for %s: %s", external_id, e, exc_info=True)
            return PostResult(success=False, error=str(e), duration_seconds=self._elapsed(_t))

    async def replace_file(self, external_id: str, file_path: str) -> PostResult:
        return PostResult(success=False, error="Weasyl does not support file replacement")

    def validate(self, package: StoryUploadPackage) -> list[str]:
        errors = super().validate(package)
        if len(package.tags) < 2:
            errors.append(f"Weasyl requires at least 2 tags (got {len(package.tags)})")
        return errors


def _rating_to_ws(rating: str) -> int:
    r = rating.lower()
    if r in ("adult", "explicit", "nsfw"):
        return 40
    elif r in ("mature", "questionable"):
        return 30
    return 10
