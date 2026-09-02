"""Itaku platform poster.

Itaku is primarily an art gallery — stories are posted as text "posts"
(max ~5000 chars) or images are uploaded to the gallery. No chapter
system, no rich formatting for literature.

Auth: requires a Django REST Framework token extracted from the user's
browser session. No OAuth, no API keys. Token stored as ik_auth_token
in settings.

Image upload: POST /api/galleries/images/ (multipart)
Text post: POST /api/posts/ (JSON)

Rating mapping:
  General → "SFW"
  Mature → "Questionable"
  Adult → "NSFW"
"""

from __future__ import annotations

import logging

import config
from clients.ik.client import IKClient
from posting import tag_budget
from posting.platforms.base import PlatformPoster, PostResult, StoryUploadPackage

logger = logging.getLogger(__name__)


# The file types Itaku treats as a gallery image rather than a text post.
# post() and edit() MUST agree — a disagreement here is the DeviantArt
# 3.34.0 crash in a different costume.
_IMAGE_TYPES = ("png", "jpg", "jpeg", "gif", "webp")
# Itaku rejects an image carrying fewer tags; its own dialog says so.
_MIN_TAGS = 5


class ItakuPoster(PlatformPoster):

    platform_id = "ik"
    platform_name = "Itaku"
    # Itaku's web client edits a gallery image through `app-edit-image-dialog`
    # — title, description, tags, folders, visibility and the maturity toggle —
    # which is `PATCH /api/galleries/images/{id}/`, the DRF sibling of the POST
    # the upload already uses, on the same token. The old "no editing via API"
    # was never checked (3.35.0).
    supports_edit = True
    # The same dialog offers "Change the source file", so a file replace IS
    # possible; it is simply not built. Left False rather than half-true —
    # `supports_*` flags that overstate are how DeviantArt ate an artwork sync
    # (3.34.0).
    supports_file_replace = False
    min_post_interval = 5
    max_file_size = 10 * 1024 * 1024  # 10 MB for images
    accepted_file_types = ["png", "jpg", "jpeg", "gif", "webp", "mp4", "webm", "mov"]

    def __init__(self):
        self._client: IKClient | None = None

    async def _ensure_client(self) -> tuple[IKClient, str]:
        """Get client and auth token."""
        settings = config.get_settings()
        creds = self._resolve_creds("ik", settings)
        target_user = creds.get("ik_target_user", "")
        token = creds.get("ik_auth_token", "")
        if not token:
            raise RuntimeError("Itaku auth token not configured (ik_auth_token)")

        if not self._client:
            self._client = IKClient(target_user)
        return self._client, token

    async def post(self, package: StoryUploadPackage) -> PostResult:
        """Upload image or create text post on Itaku."""
        _t = self._start_timer()
        try:
            client, token = await self._ensure_client()

            rating = _rating_to_ik(package.rating)

            # If file is an image, upload to gallery
            if package.file_path and package.file_type in _IMAGE_TYPES:
                result = await client.upload_image(
                    package.file_path,
                    title=package.title,
                    description=package.description[:5000],
                    tags=tag_budget.fit(package.tags, self.platform_id),
                    maturity_rating=rating,
                    token=token,
                )
                return PostResult(
                    success=True,
                    external_id=result.get("id", ""),
                    external_url=result.get("url", ""),
                    duration_seconds=self._elapsed(_t),
                )

            # Otherwise create a text post
            result = await client.create_post(
                title=package.title,
                content=package.description[:5000],
                tags=tag_budget.fit(package.tags, self.platform_id),
                maturity_rating=rating,
                token=token,
            )
            return PostResult(
                success=True,
                external_id=result.get("id", ""),
                external_url=result.get("url", ""),
                duration_seconds=self._elapsed(_t),
            )

        except Exception as e:
            logger.error("IK post failed: %s", e, exc_info=True)
            return PostResult(success=False, error=str(e), duration_seconds=self._elapsed(_t))

    async def edit(self, external_id: str, package: StoryUploadPackage) -> PostResult:
        """Push canonical metadata to an existing Itaku gallery image.

        `PATCH /api/galleries/images/{id}/` with the same fields the upload
        sends and the same token. Unlike e621, Itaku **has a title** (capped at
        100 by its own dialog) and its tags belong to the image's owner rather
        than the community, so a straight replace is the right semantics here.

        ⚠ **Gallery images only.** `post()` also creates Itaku *text posts*
        (`create_post`) when a package carries no image, and those live under a
        different endpoint whose edit has not been verified. Rather than let a
        text package fall through to the image endpoint — the exact shape of
        the DeviantArt artwork crash (3.34.0) — it is refused explicitly.

        ⚠ **Never re-shares to the feed.** `edit_image` omits `share_on_feed`
        entirely; sending it would push the piece back onto every follower's
        activity feed on each metadata sync.

        `extra["skip_content_refresh"]` is accepted and ignored — the image is
        never re-uploaded here, which is what Sync-all wants anyway.
        """
        _t = self._start_timer()
        try:
            if package.file_type not in _IMAGE_TYPES:
                return PostResult(
                    success=False,
                    external_id=external_id,
                    error=("Itaku edit covers gallery images only — a text post's "
                           "edit endpoint has not been verified."),
                    duration_seconds=self._elapsed(_t),
                )

            tags = tag_budget.fit(package.tags, self.platform_id)
            if len(tags) < _MIN_TAGS:
                # Itaku's own floor. Refuse here so the caller gets a clean
                # failed result instead of the API's 400, and so a thin set can
                # never strip a well-tagged live image.
                return PostResult(
                    success=False,
                    external_id=external_id,
                    error=(f"Itaku requires at least {_MIN_TAGS} tags "
                           f"(got {len(tags)})"),
                    duration_seconds=self._elapsed(_t),
                )

            client, token = await self._ensure_client()

            result = await client.edit_image(
                external_id,
                title=package.title,
                description=package.description[:5000],
                tags=tags,
                maturity_rating=_rating_to_ik(package.rating),
                token=token,
            )

            return PostResult(
                success=True,
                external_id=external_id,
                external_url=result.get("url", ""),
                duration_seconds=self._elapsed(_t),
            )
        except Exception as e:
            logger.error("IK edit failed for %s: %s", external_id, e, exc_info=True)
            return PostResult(success=False, external_id=external_id, error=str(e),
                              duration_seconds=self._elapsed(_t))

    async def replace_file(self, external_id: str, file_path: str) -> PostResult:
        """Itaku doesn't support file replacement."""
        return PostResult(success=False, error="Itaku does not support file replacement")

    def validate(self, package: StoryUploadPackage) -> list[str]:
        errors = []
        if len(package.tags) < _MIN_TAGS:
            errors.append(f"Itaku requires at least {_MIN_TAGS} tags "
                          f"(got {len(package.tags)})")
        if package.file_path:
            import os
            if os.path.isfile(package.file_path):
                size = os.path.getsize(package.file_path)
                if size > self.max_file_size:
                    errors.append(f"File too large: {size / 1024 / 1024:.1f}MB (max 10MB)")
        return errors


def _rating_to_ik(rating: str) -> str:
    r = rating.lower()
    if r in ("adult", "explicit", "nsfw"):
        return "NSFW"
    elif r in ("mature", "questionable"):
        return "Questionable"
    return "SFW"
