"""SoundCloud platform poster — MEDIAPLATS §3 (4.22.0).

An audio piece becomes a track: ``POST /tracks`` (multipart) with the title, the
description, the tags as SoundCloud's ``tag_list``, ``sharing`` public / private from the
piece's ``extra["private"]``, an optional genre from ``extra["sc_genre"]``, and the poster
fitted to a square as the cover (SoundCloud wants at least 800 × 800). Edits are real:
``PUT /tracks/{id}`` carries the same fields; the audio itself can never be replaced.

**Rating.** ``max_rating = "mature"`` — SoundCloud permits explicit lyrics and forbids
pornographic audio, so an ``adult`` piece is refused with the reason before anything is
sent, and ``mature`` passes (the shared gate, 4.21.0).

**Tokens.** The account's OAuth pair is resolved through ``_resolve_creds`` and the
rotated pair is written back through ``_save_creds`` straight after any call that could
have refreshed — before the upload, not after it, so a failed upload cannot lose the
token SoundCloud already spent.

❓ Not proven live: app registration needs an Artist Pro subscription and SoundCloud's
approval; the request shapes are the documented ones (clients/sc/client.py).
"""
from __future__ import annotations

import logging
import os

import config
from clients.sc.client import ACCEPTED_AUDIO, ScClient
from posting import poster_fit
from posting.platforms.base import PlatformPoster, PostResult, StoryUploadPackage

logger = logging.getLogger(__name__)

COVER_SIDE = 1000
MAX_TRACK_BYTES = 4 * 1024 * 1024 * 1024
MAX_TRACK_SECONDS = 24 * 3600


class SoundCloudPoster(PlatformPoster):

    platform_id = "sc"
    platform_name = "SoundCloud"
    supports_edit = True
    supports_artwork_edit = True
    supports_file_replace = False
    min_post_interval = 10
    max_file_size = MAX_TRACK_BYTES
    accepted_file_types = list(ACCEPTED_AUDIO)
    accepted_media = {"image": [], "video": [], "audio": list(ACCEPTED_AUDIO)}
    max_rating = "mature"
    requires_mode = "any"

    def __init__(self):
        self._client: ScClient | None = None

    def _ensure_client(self) -> ScClient:
        from polling.sc_poller import client_from_creds
        creds = self._resolve_creds("sc", config.get_settings())
        if not (creds.get("sc_client_id") and creds.get("sc_client_secret")):
            raise RuntimeError("SoundCloud app credentials not configured "
                               "(Settings → Platforms → SoundCloud)")
        if not creds.get("sc_refresh_token"):
            raise RuntimeError("SoundCloud is not authorised — connect it in Settings")
        if self._client is None or self._client.refresh_token != creds.get("sc_refresh_token"):
            self._client = client_from_creds(creds)
        return self._client

    def _persist_tokens(self) -> None:
        from polling.sc_poller import token_updates
        client = self._client
        if client is None or not client.tokens_changed:
            return
        try:
            self._save_creds("sc", token_updates(client))
            client.tokens_changed = False
        except Exception:
            logger.debug("SoundCloud token persist failed", exc_info=True)

    @staticmethod
    def _cover(package: StoryUploadPackage) -> str | None:
        src = package.thumbnail_path
        if not src or not os.path.isfile(src):
            return None
        try:
            return poster_fit.square(src, COVER_SIDE)
        except Exception:
            logger.debug("SoundCloud cover fit failed", exc_info=True)
            return None

    @staticmethod
    def _sharing(package: StoryUploadPackage) -> str:
        return "private" if (package.extra or {}).get("private") else "public"

    async def post(self, package: StoryUploadPackage) -> PostResult:
        _t = self._start_timer()
        cover = None
        try:
            client = self._ensure_client()
            await client.refresh()          # a fresh access token for a long upload
            self._persist_tokens()          # immediately: the old refresh token is spent
            cover = self._cover(package)
            result = await client.upload_track(
                file_path=package.file_path or "", title=package.title or "",
                description=package.description or "", tags=list(package.tags or []),
                genre=str((package.extra or {}).get("sc_genre", "") or ""),
                sharing=self._sharing(package), artwork_path=cover,
                artist=str((package.extra or {}).get("artist", "") or ""))
            self._persist_tokens()
            if not result.get("success"):
                return PostResult(success=False, error=result.get("error", "upload failed"),
                                  duration_seconds=self._elapsed(_t))
            return PostResult(success=True, external_id=str(result.get("id", "")),
                              external_url=result.get("url", ""), duration_seconds=self._elapsed(_t))
        except Exception as e:
            self._persist_tokens()
            logger.error("SoundCloud post failed: %s", e, exc_info=True)
            return PostResult(success=False, error=str(e), duration_seconds=self._elapsed(_t))
        finally:
            if cover:
                try:
                    os.remove(cover)
                except OSError:
                    pass

    async def edit(self, external_id: str, package: StoryUploadPackage) -> PostResult:
        _t = self._start_timer()
        cover = None
        try:
            client = self._ensure_client()
            cover = self._cover(package)
            result = await client.update_track(
                external_id, title=package.title or "", description=package.description or "",
                tags=list(package.tags or []), genre=str((package.extra or {}).get("sc_genre", "") or "") or None,
                sharing=self._sharing(package), artwork_path=cover)
            self._persist_tokens()
            if not result.get("success"):
                return PostResult(success=False, error=result.get("error", "update failed"),
                                  duration_seconds=self._elapsed(_t))
            return PostResult(success=True, external_id=str(result.get("id") or external_id),
                              external_url=result.get("url", ""), duration_seconds=self._elapsed(_t))
        except Exception as e:
            self._persist_tokens()
            logger.error("SoundCloud edit failed: %s", e, exc_info=True)
            return PostResult(success=False, error=str(e), duration_seconds=self._elapsed(_t))
        finally:
            if cover:
                try:
                    os.remove(cover)
                except OSError:
                    pass

    async def replace_file(self, external_id: str, file_path: str) -> PostResult:
        return PostResult(success=False, error="SoundCloud cannot replace a track's audio — upload a new track")

    def validate(self, package: StoryUploadPackage) -> list[str]:
        errors: list[str] = []
        if not package.file_path:
            errors.append("SoundCloud needs an audio file")
        if not package.title:
            errors.append("Title is required")
        if package.duration_s and package.duration_s > MAX_TRACK_SECONDS:
            errors.append("SoundCloud takes tracks up to 24 hours")
        if package.file_path and os.path.isfile(package.file_path):
            if os.path.getsize(package.file_path) > MAX_TRACK_BYTES:
                errors.append("SoundCloud takes files up to 4 GB")
        creds = self._resolve_creds("sc", config.get_settings())
        if not (creds.get("sc_client_id") and creds.get("sc_client_secret")):
            errors.append("SoundCloud app credentials are not configured (Settings → Platforms → SoundCloud)")
        elif not creds.get("sc_refresh_token"):
            errors.append("SoundCloud is not authorised for this account — Connect in Settings")
        refusal = self.refusal(package)
        if refusal:
            errors.append(refusal)
        return errors
