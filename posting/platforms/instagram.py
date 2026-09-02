"""Instagram artwork poster — 2.139.0.

Makes Instagram a first-class *artwork* publish target (previously IG could only
be reached through the Posts / microblog module). It posts a single image via the
Graph API Content Publishing flow, reusing the exact public-image-hosting trick
the Posts module already uses (``posting.ig_media``): Instagram never accepts raw
image bytes — you hand Meta a **public image URL** and its servers cURL it. So we

  • Server:  stash a web-safe JPEG on the data volume and serve it at
             ``/api/ig/pubmedia/<token>`` (needs ``IG_PUBLIC_BASE_URL``), or
  • Desktop: relay the image to a paired server which hosts it,

publish the container, then delete the stash. Same public-address constraint as
IG Posts — hence a clear validate() error when no host is configured.

Caption = the artwork description (falling back to the title), with the tag set
appended as sanitised hashtags (Instagram's discovery mechanism; capped at 30).
IG has no photo edit/replace API, so this poster is post-only (like Bluesky /
e621 / Itaku) — no sync-in-place.
"""
from __future__ import annotations

import logging

import config
from posting.platforms.base import PlatformPoster, PostResult, StoryUploadPackage

logger = logging.getLogger(__name__)

_MAX_HASHTAGS = 30


class InstagramPoster(PlatformPoster):

    platform_id = "ig"
    platform_name = "Instagram"
    supports_edit = False
    supports_file_replace = False
    min_post_interval = 5
    # IG's photo cap is 8 MB, but ig_media downscales the long edge to 1440px and
    # re-encodes JPEG q88 before hosting, so the stashed image is always in range.
    # We therefore don't reject a large source in validate() — it'll be shrunk.
    max_file_size = 0
    accepted_file_types = ["jpg", "jpeg", "png", "webp"]
    requires_mode = "any"     # works server-side; desktop relays to a paired server

    async def post(self, package: StoryUploadPackage) -> PostResult:
        """Publish one image to Instagram."""
        _t = self._start_timer()
        stashed: list[str] = []
        try:
            settings = config.get_settings()
            creds = self._resolve_creds("ig", settings)
            token = creds.get("ig_access_token", "")
            if not token:
                return PostResult(success=False, error="Instagram account isn't connected",
                                  duration_seconds=self._elapsed(_t))

            # Instagram fetches the image from a public URL — resolve one the same
            # way the Posts module does (local public host, or relay to a server).
            local_base = settings.get("ig_public_base_url", "").strip()
            relay_url = settings.get("posting_server_url", "").strip()
            relay_key = settings.get("posting_server_api_key", "").strip()
            if not local_base and not relay_url:
                return PostResult(success=False, error=(
                    "Instagram posting needs a public address for Meta to fetch the image. "
                    "On the server set IG_PUBLIC_BASE_URL; on the desktop app pair it with your "
                    "server (Settings → Posting) so it can host the image."),
                    duration_seconds=self._elapsed(_t))

            from posting import ig_media
            from posting.post_publisher import _relay_stash_image
            from clients.ig.client import IgClient

            path = package.file_path or ""
            if local_base:
                tok = ig_media.stash_image(path)
                stashed.append(tok)
                image_urls = [ig_media.public_url(local_base, tok)]
            else:
                image_urls = [await _relay_stash_image(relay_url, relay_key, path)]

            caption = _build_caption(package)
            client = IgClient(access_token=token, user_id=creds.get("ig_user_id", ""))
            try:
                r = await client.create_post(caption, image_urls)
            finally:
                await client.close()

            if r and r.get("id"):
                return PostResult(success=True, external_id=str(r["id"]),
                                  external_url=r.get("url", ""),
                                  duration_seconds=self._elapsed(_t))
            return PostResult(success=False,
                              error="Instagram rejected the post (check the token / logs)",
                              duration_seconds=self._elapsed(_t))
        except Exception as e:
            logger.error("Instagram artwork post failed: %s", e, exc_info=True)
            return PostResult(success=False, error=str(e),
                              duration_seconds=self._elapsed(_t))
        finally:
            # Only LOCAL stashes are ours to clean; relayed images self-expire on
            # the hosting server's TTL sweep.
            if stashed:
                from posting import ig_media
                for tok in stashed:
                    ig_media.cleanup(tok)

    async def edit(self, external_id: str, package: StoryUploadPackage) -> PostResult:
        return PostResult(success=False, error="Instagram does not support editing via API")

    async def replace_file(self, external_id: str, file_path: str) -> PostResult:
        return PostResult(success=False, error="Instagram does not support file replacement")

    def validate(self, package: StoryUploadPackage) -> list[str]:
        errors: list[str] = []
        if not package.file_path:
            errors.append("Instagram requires an image file")
        else:
            import os
            if not os.path.isfile(package.file_path):
                errors.append(f"File not found: {package.file_path}")
        # Fail fast (before any stash) when no public host is configured.
        s = config.get_settings()
        if not s.get("ig_public_base_url", "").strip() and not s.get("posting_server_url", "").strip():
            errors.append("Instagram needs a public image host — set IG_PUBLIC_BASE_URL on the "
                          "server, or pair the desktop app with your server (Settings → Posting)")
        return errors


def _build_caption(package: StoryUploadPackage) -> str:
    """Instagram caption: the description (or title) + hashtagged tags below it."""
    parts: list[str] = []
    body = (package.description or package.title or "").strip()
    if body:
        parts.append(body)
    tags = _hashtags(package.tags)
    if tags:
        parts.append(tags)
    return "\n\n".join(parts)


def _hashtags(tags: list[str]) -> str:
    """Turn a tag list into Instagram hashtags — alnum/underscore only, deduped,
    capped at Instagram's 30-hashtag ceiling."""
    seen: set[str] = set()
    out: list[str] = []
    for t in tags or []:
        h = "".join(ch for ch in t if ch.isalnum() or ch == "_")
        if not h or h.lower() in seen:
            continue
        seen.add(h.lower())
        out.append("#" + h)
        if len(out) >= _MAX_HASHTAGS:
            break
    return " ".join(out)
