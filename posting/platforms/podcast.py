"""Podcast feed poster — an audio piece becomes an episode of a feed PawPoller serves itself.

MEDIAPLATS §4 (4.21.1). Nothing is uploaded anywhere: ``post()`` inserts an episode row and
the feed's RSS (``posting/podcast_feed.py``, served by ``routes/podcast_api.py`` at
``/feed/{slug}.xml``) lists it on the next fetch by Apple Podcasts, Spotify, Amazon Music,
Pocket Casts or any player. The piece's own file is streamed from the artwork archive
through a per-feed public path, with Range, so the audio never moves either.

**One account per feed** — the way Telegram holds one account per channel. The account's
single credential field, ``pod_feed_slug``, names the feed; the podcasts page creates the
feed and its account together. A poster resolves its feed through the ordinary
``_resolve_creds`` path, so a persona's feed is picked the way a persona's channel is.

**Server-only.** A feed is fetched at a public address, and the episode row has to be in
the database that address is served from — ``requires_mode = "server"`` routes a desktop
post through the paired server exactly as the scheduler does for any server-only job.

Edits are real: an episode's title, notes and explicit flag are its own row, so
"Sync to sites" updates the feed. Unpublishing removes the row; the piece stays.
"""
from __future__ import annotations

import logging

import config
from database import podcasts as pdb
from posting import podcast_feed
from posting.platforms.base import PlatformPoster, PostResult, StoryUploadPackage, rating_rank

logger = logging.getLogger(__name__)

AUDIO_TYPES = ["mp3", "wav", "flac", "ogg", "m4a", "aac", "opus"]


def public_base(settings: dict | None = None) -> str:
    """The address a feed is served at: the Instagram host's public base is the same
    requirement (a public install), so it is reused rather than a second setting."""
    s = settings if settings is not None else config.get_settings()
    return (s.get("ig_public_base_url") or "").strip().rstrip("/")


class PodcastPoster(PlatformPoster):

    platform_id = "pod"
    platform_name = "Podcast feed"
    supports_edit = True
    supports_artwork_edit = True
    supports_file_replace = False
    min_post_interval = 0
    max_file_size = 0
    requires_mode = "server"
    accepted_file_types = list(AUDIO_TYPES)
    accepted_media = {"image": [], "video": [], "audio": list(AUDIO_TYPES)}
    max_rating = "adult"                    # any rating; the episode carries the explicit flag

    def _feed(self, settings: dict | None = None) -> tuple[dict | None, str]:
        """(feed row, slug) for THIS poster's account, or (None, slug) when unset / unknown."""
        creds = self._resolve_creds("pod", settings)
        slug = (creds.get("pod_feed_slug") or "").strip()
        if not slug:
            return None, ""
        from database.db import get_connection
        conn = get_connection()
        try:
            return pdb.get_feed_by_slug(conn, slug), slug
        finally:
            conn.close()

    async def post(self, package: StoryUploadPackage) -> PostResult:
        _t = self._start_timer()
        try:
            settings = config.get_settings()
            feed, slug = self._feed(settings)
            if not slug:
                return PostResult(success=False, duration_seconds=self._elapsed(_t),
                                  error="This account has no podcast feed — make one on the Podcasts page")
            if not feed:
                return PostResult(success=False, duration_seconds=self._elapsed(_t),
                                  error=f"The podcast feed '{slug}' no longer exists")
            piece = package.story_name or ""
            if not piece:
                return PostResult(success=False, duration_seconds=self._elapsed(_t),
                                  error="No piece to publish")
            x = package.extra or {}
            explicit = bool(x.get("explicit")) if x.get("explicit") is not None else rating_rank(package.rating) >= 2
            from database.db import get_connection
            conn = get_connection()
            try:
                ep = pdb.add_episode(conn, feed_id=feed["feed_id"], artwork_name=piece,
                                     title=package.title or piece, notes=package.description or "",
                                     explicit=explicit,
                                     season=_int_or_none(x.get("season")),
                                     episode_number=_int_or_none(x.get("episode_number")))
            finally:
                conn.close()
            base = public_base(settings)
            url = podcast_feed.episode_page_url(base, slug, ep["guid"]) if base else ""
            logger.info("Podcast: episode %s in feed %s — %s", ep["guid"][:8], slug, (package.title or piece)[:40])
            return PostResult(success=True, external_id=ep["guid"], external_url=url,
                              duration_seconds=self._elapsed(_t))
        except Exception as e:
            logger.error("Podcast post failed: %s", e, exc_info=True)
            return PostResult(success=False, error=str(e), duration_seconds=self._elapsed(_t))

    async def edit(self, external_id: str, package: StoryUploadPackage) -> PostResult:
        _t = self._start_timer()
        try:
            feed, slug = self._feed()
            if not feed:
                return PostResult(success=False, external_id=external_id, duration_seconds=self._elapsed(_t),
                                  error="This account's podcast feed no longer exists")
            from database.db import get_connection
            conn = get_connection()
            try:
                ep = pdb.get_episode_by_guid(conn, feed["feed_id"], external_id)
                if not ep:
                    return PostResult(success=False, external_id=external_id, duration_seconds=self._elapsed(_t),
                                      error="That episode is no longer in the feed")
                x = package.extra or {}
                fields = {"title": package.title or ep["title"], "notes": package.description or ""}
                if x.get("explicit") is not None:
                    fields["explicit"] = bool(x.get("explicit"))
                else:
                    fields["explicit"] = rating_rank(package.rating) >= 2
                pdb.update_episode(conn, ep["episode_id"], **fields)
            finally:
                conn.close()
            base = public_base()
            return PostResult(success=True, external_id=external_id,
                              external_url=podcast_feed.episode_page_url(base, slug, external_id) if base else "",
                              duration_seconds=self._elapsed(_t))
        except Exception as e:
            logger.error("Podcast edit failed: %s", e, exc_info=True)
            return PostResult(success=False, external_id=external_id, error=str(e), duration_seconds=self._elapsed(_t))

    async def replace_file(self, external_id: str, file_path: str) -> PostResult:
        return PostResult(success=False, error="An episode plays the piece's own file — replace the piece's audio instead")

    def validate(self, package: StoryUploadPackage) -> list[str]:
        errors = super().validate(package)
        settings = config.get_settings()
        if not public_base(settings):
            errors.append("A podcast feed needs this install to be reachable at a public address — "
                          "set IG_PUBLIC_BASE_URL on the server")
        try:
            feed, slug = self._feed(settings)
        except Exception:
            feed, slug = None, ""
        if not slug:
            errors.append("This account has no podcast feed — make one on the Podcasts page")
        elif not feed:
            errors.append(f"The podcast feed '{slug}' no longer exists")
        return errors


def _int_or_none(v):
    try:
        return int(v) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None
