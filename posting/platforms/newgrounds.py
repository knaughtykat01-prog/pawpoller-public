"""Newgrounds platform poster — MEDIAPLATS §5 (4.23.0). Cookie-and-form, three portals.

One poster class with a branch on the piece's kind: audio → the Audio Portal, video → the
Movie Portal, image → the Art Portal (4.29.0; ``clients/ng/client.py`` ``submit_project``). The poster becomes the project's
icon (fitted square); tags follow the site's rules; the rating maps onto the four content
descriptors Newgrounds derives E / T / M / A from (``extra["ng_nudity"]`` etc. override one);
the genre comes from ``extra["ng_genre"]`` (an id from the client's lists). A published
piece enters **Under Judgment** — the result is "submitted"; the poller reads what the
public decided. Edits go through the project's edit page.

**Whose session is this?** The cookie pair is checked against the account's ``ng_username``
before any post (the 3.31.0 / 4.6.3 FurAffinity lesson): a valid session for the wrong
account is the one mistake that posts to someone else's page.

``max_rating = "adult"`` — Newgrounds has a full A rating behind an age gate.
❓ Not proven live: the portal form field names are by analogy with the art portal's
(PostyBirb); see the client's ``PORTALS`` table.
"""
from __future__ import annotations

import logging
import os

import config
from clients.ng.client import (ART_MAX_BYTES, ART_TYPES, AUDIO_MAX_BYTES, AUDIO_TYPES, MOVIE_MAX_BYTES,
                               MOVIE_TYPES, NgClient, portal_for)
from posting import poster_fit
from posting.platforms.base import PlatformPoster, PostResult, StoryUploadPackage

logger = logging.getLogger(__name__)

ICON_SIDE = 600


class NewgroundsPoster(PlatformPoster):

    platform_id = "ng"
    platform_name = "Newgrounds"
    supports_edit = True
    supports_artwork_edit = True
    supports_file_replace = False
    min_post_interval = 30
    max_file_size = MOVIE_MAX_BYTES
    accepted_file_types = list(AUDIO_TYPES) + list(MOVIE_TYPES) + list(ART_TYPES)
    accepted_media = {"image": list(ART_TYPES), "video": list(MOVIE_TYPES), "audio": list(AUDIO_TYPES)}
    max_rating = "adult"
    requires_mode = "any"

    def __init__(self):
        self._client: NgClient | None = None
        self._client_creds: tuple | None = None

    async def _ensure_client(self) -> NgClient:
        creds = self._resolve_creds("ng", config.get_settings())
        username, cookie = (creds.get("ng_username") or "").strip(), (creds.get("ng_cookie") or "").strip()
        if not cookie:
            raise RuntimeError("Newgrounds is not connected (Settings → Platforms → Newgrounds — log in via the browser)")
        fp = (username, cookie)
        if self._client is not None and self._client_creds == fp:
            return self._client
        client = NgClient(username=username, cookie=cookie)
        session = await client.validate_session()
        if not session.get("ok"):
            await client.close()
            raise RuntimeError(f"Newgrounds session check failed: {session.get('detail') or 'log in again'}")
        self._client, self._client_creds = client, fp
        return client

    @staticmethod
    def _icon(package: StoryUploadPackage) -> str | None:
        src = package.thumbnail_path
        if not src or not os.path.isfile(src):
            return None
        try:
            return poster_fit.square(src, ICON_SIDE)
        except Exception:
            logger.debug("Newgrounds icon fit failed", exc_info=True)
            return None

    @staticmethod
    def _portal(package: StoryUploadPackage) -> str | None:
        return portal_for(package.media_kind, package.file_type or os.path.splitext(package.file_path or "")[1])

    async def post(self, package: StoryUploadPackage) -> PostResult:
        _t = self._start_timer()
        icon = None
        try:
            portal = self._portal(package)
            if not portal:
                return PostResult(success=False, error="Newgrounds takes audio (Audio Portal), video (Movie Portal) or an image (Art Portal)",
                                  duration_seconds=self._elapsed(_t))
            client = await self._ensure_client()
            icon = self._icon(package)
            extra = package.extra or {}
            result = await client.submit_project(
                portal, file_path=package.file_path or "", title=package.title or "",
                description=package.description or "", tags=list(package.tags or []),
                genre_id=str(extra.get("ng_genre", "") or ""), rating=package.rating or "general",
                icon_path=icon, extra=extra)
            if not result.get("success"):
                return PostResult(success=False, error=result.get("error", "submission failed"),
                                  duration_seconds=self._elapsed(_t))
            return PostResult(success=True, external_id=f"{portal}:{result.get('id', '')}",
                              external_url=result.get("url", ""), duration_seconds=self._elapsed(_t))
        except Exception as e:
            logger.error("Newgrounds post failed: %s", e, exc_info=True)
            return PostResult(success=False, error=str(e), duration_seconds=self._elapsed(_t))
        finally:
            if icon:
                try:
                    os.remove(icon)
                except OSError:
                    pass

    @staticmethod
    def split_external_id(external_id: str) -> tuple[str, str]:
        """'audio:123' → ('audio', '123'); a bare id is assumed to be audio."""
        if ":" in (external_id or ""):
            portal, pid = external_id.split(":", 1)
            return (portal if portal in ("audio", "movie", "art") else "audio"), pid
        return "audio", external_id or ""

    async def edit(self, external_id: str, package: StoryUploadPackage) -> PostResult:
        _t = self._start_timer()
        try:
            portal, pid = self.split_external_id(external_id)
            client = await self._ensure_client()
            extra = package.extra or {}
            result = await client.update_project(
                portal, pid, title=package.title or "", description=package.description or "",
                tags=list(package.tags or []), genre_id=str(extra.get("ng_genre", "") or "") or None,
                rating=package.rating or "general", extra=extra)
            if not result.get("success"):
                return PostResult(success=False, error=result.get("error", "edit failed"),
                                  duration_seconds=self._elapsed(_t))
            return PostResult(success=True, external_id=external_id, external_url=result.get("url", ""),
                              duration_seconds=self._elapsed(_t))
        except Exception as e:
            logger.error("Newgrounds edit failed: %s", e, exc_info=True)
            return PostResult(success=False, error=str(e), duration_seconds=self._elapsed(_t))

    async def replace_file(self, external_id: str, file_path: str) -> PostResult:
        return PostResult(success=False, error="Newgrounds cannot replace a submission's file — submit a new project")

    def validate(self, package: StoryUploadPackage) -> list[str]:
        errors: list[str] = []
        if not package.file_path:
            errors.append("Newgrounds needs an audio, video or image file")
        if not package.title:
            errors.append("Title is required")
        portal = self._portal(package)
        if package.file_path and os.path.isfile(package.file_path) and portal:
            size = os.path.getsize(package.file_path)
            cap = {"audio": AUDIO_MAX_BYTES, "movie": MOVIE_MAX_BYTES, "art": ART_MAX_BYTES}[portal]
            if size > cap:
                errors.append(f"Newgrounds' {portal.title()} Portal takes files up to {cap // (1024 * 1024)} MB")
        creds = self._resolve_creds("ng", config.get_settings())
        if not (creds.get("ng_cookie") or "").strip():
            errors.append("Newgrounds is not connected (Settings → Platforms → Newgrounds — log in via the browser)")
        refusal = self.refusal(package)
        if refusal:
            errors.append(refusal)
        return errors
