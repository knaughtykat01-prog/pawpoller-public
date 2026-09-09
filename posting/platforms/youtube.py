"""YouTube platform poster — MEDIAPLATS §6 (4.24.0).

A video piece becomes an upload: the resumable ``videos.insert`` with the title (100 chars),
description, tags, ``categoryId`` from ``extra["yt_category"]`` (default Film & Animation),
``privacyStatus`` from ``extra["yt_privacy"]`` (default private — see below), made-for-kids
false; then ``thumbnails.set`` with the poster fitted to 16:9 1280 × 720 JPEG (a custom
thumbnail needs a phone-verified channel; a refusal there is reported in the result, not
fatal). Edits are real (``videos.update``: title / description / tags / category / privacy);
the file can never be replaced.

**Private until audited.** Every upload from an unverified Google project created after 28 July
2020 is restricted to private viewing whatever ``privacyStatus`` asks for. The poster asks for
what the piece wants, reads back what YouTube set, and says so in the result's URL line — the
operator flips it public in Studio, or passes YouTube's API compliance audit.

**Rating.** ``max_rating = "mature"`` — YouTube forbids sexually explicit content, so an
``adult`` piece is refused with the reason and ``mature`` passes (age-restriction is YouTube's
own call after upload).

**Tokens.** Google's refresh token is stable but the access token is written back after any
call that refreshed, so the poller and the poster share one; a *Testing*-mode app's refresh
token dies after 7 days and the client's error says so.
"""
from __future__ import annotations

import logging
import os

import config
from clients.yt.client import ACCEPTED_VIDEO, CATEGORIES, DEFAULT_CATEGORY, MAX_BYTES, PRIVACY, YtClient
from posting import poster_fit
from posting.platforms.base import PlatformPoster, PostResult, StoryUploadPackage

logger = logging.getLogger(__name__)

THUMB_WIDTH = 1280
MIME = {"mp4": "video/mp4", "m4v": "video/x-m4v", "webm": "video/webm", "mov": "video/quicktime"}


class YouTubePoster(PlatformPoster):

    platform_id = "yt"
    platform_name = "YouTube"
    supports_edit = True
    supports_artwork_edit = True
    supports_file_replace = False
    min_post_interval = 30
    max_file_size = MAX_BYTES
    accepted_file_types = list(ACCEPTED_VIDEO)
    accepted_media = {"image": [], "video": list(ACCEPTED_VIDEO), "audio": []}
    max_rating = "mature"
    requires_mode = "any"

    def __init__(self):
        self._client: YtClient | None = None

    def _ensure_client(self) -> YtClient:
        from polling.yt_poller import client_from_creds
        creds = self._resolve_creds("yt", config.get_settings())
        if not (creds.get("yt_client_id") and creds.get("yt_client_secret")):
            raise RuntimeError("YouTube app credentials not configured (Settings → Platforms → YouTube)")
        if not creds.get("yt_refresh_token"):
            raise RuntimeError("YouTube is not authorised — connect it in Settings")
        if self._client is None or self._client.refresh_token != creds.get("yt_refresh_token"):
            self._client = client_from_creds(creds)
        return self._client

    def _persist_tokens(self) -> None:
        from polling.yt_poller import token_updates
        client = self._client
        if client is None or not client.tokens_changed:
            return
        try:
            self._save_creds("yt", token_updates(client))
            client.tokens_changed = False
        except Exception:
            logger.debug("YouTube token persist failed", exc_info=True)

    @staticmethod
    def _thumb(package: StoryUploadPackage) -> str | None:
        src = package.thumbnail_path
        if not src or not os.path.isfile(src):
            return None
        try:
            return poster_fit.widescreen(src, THUMB_WIDTH, max_bytes=2 * 1024 * 1024)
        except Exception:
            logger.debug("YouTube thumbnail fit failed", exc_info=True)
            return None

    @staticmethod
    def _privacy(package: StoryUploadPackage) -> str:
        want = str((package.extra or {}).get("yt_privacy", "") or "").lower()
        if want in PRIVACY:
            return want
        return "private" if (package.extra or {}).get("private", True) else "public"

    @staticmethod
    def _category(package: StoryUploadPackage) -> str:
        cat = str((package.extra or {}).get("yt_category", "") or "")
        return cat if cat in CATEGORIES else DEFAULT_CATEGORY

    async def post(self, package: StoryUploadPackage) -> PostResult:
        _t = self._start_timer()
        thumb = None
        try:
            client = self._ensure_client()
            ext = (package.file_type or os.path.splitext(package.file_path or "")[1].lstrip(".")).lower()
            result = await client.upload_video(
                file_path=package.file_path or "", title=package.title or "", description=package.description or "",
                tags=list(package.tags or []), category_id=self._category(package), privacy=self._privacy(package),
                made_for_kids=bool((package.extra or {}).get("yt_made_for_kids", False)),
                mime=MIME.get(ext, "video/*"))
            self._persist_tokens()
            if not result.get("success"):
                return PostResult(success=False, error=result.get("error", "upload failed"),
                                  duration_seconds=self._elapsed(_t))
            vid = str(result.get("id", ""))
            note = ""
            thumb = self._thumb(package)
            if thumb:
                t = await client.set_thumbnail(vid, thumb)
                if not t.get("success"):
                    note = f" (thumbnail not set: {t.get('error')})"
            got = result.get("privacy", "")
            wanted = self._privacy(package)
            if got and got != wanted:
                note += f" (YouTube set it {got}: an unaudited project's uploads stay private — flip it in Studio)"
            elif got == "private":
                note += " (private — flip it in Studio when ready)"
            self._persist_tokens()
            return PostResult(success=True, external_id=vid, external_url=(result.get("url", "") + note).strip(),
                              duration_seconds=self._elapsed(_t))
        except Exception as e:
            self._persist_tokens()
            logger.error("YouTube post failed: %s", e, exc_info=True)
            return PostResult(success=False, error=str(e), duration_seconds=self._elapsed(_t))
        finally:
            if thumb:
                try:
                    os.remove(thumb)
                except OSError:
                    pass

    async def edit(self, external_id: str, package: StoryUploadPackage) -> PostResult:
        _t = self._start_timer()
        thumb = None
        try:
            client = self._ensure_client()
            extra = package.extra or {}
            privacy = str(extra.get("yt_privacy", "") or "").lower()
            result = await client.update_video(
                external_id, title=package.title or "", description=package.description or "",
                tags=list(package.tags or []), category_id=str(extra.get("yt_category", "") or "") or None,
                privacy=privacy if privacy in PRIVACY else None)
            self._persist_tokens()
            if not result.get("success"):
                return PostResult(success=False, error=result.get("error", "update failed"),
                                  duration_seconds=self._elapsed(_t))
            thumb = self._thumb(package)
            if thumb:
                await client.set_thumbnail(external_id, thumb)
            return PostResult(success=True, external_id=external_id, external_url=result.get("url", ""),
                              duration_seconds=self._elapsed(_t))
        except Exception as e:
            self._persist_tokens()
            logger.error("YouTube edit failed: %s", e, exc_info=True)
            return PostResult(success=False, error=str(e), duration_seconds=self._elapsed(_t))
        finally:
            if thumb:
                try:
                    os.remove(thumb)
                except OSError:
                    pass

    async def replace_file(self, external_id: str, file_path: str) -> PostResult:
        return PostResult(success=False, error="YouTube cannot replace a video's file — upload a new video")

    def validate(self, package: StoryUploadPackage) -> list[str]:
        errors: list[str] = []
        if not package.file_path:
            errors.append("YouTube needs a video file")
        if not package.title:
            errors.append("Title is required")
        if package.file_path and os.path.isfile(package.file_path) and os.path.getsize(package.file_path) > MAX_BYTES:
            errors.append("YouTube takes files up to 256 GB")
        creds = self._resolve_creds("yt", config.get_settings())
        if not (creds.get("yt_client_id") and creds.get("yt_client_secret")):
            errors.append("YouTube app credentials are not configured (Settings → Platforms → YouTube)")
        elif not creds.get("yt_refresh_token"):
            errors.append("YouTube is not authorised for this account — Connect in Settings")
        refusal = self.refusal(package)
        if refusal:
            errors.append(refusal)
        return errors
