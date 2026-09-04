"""X (Twitter) poster — artwork and story announcements (4.3.7).

X could be polled, and posted to from the Posts / microblog hub, but it was
not in the artwork picker: ``Artwork._PLATFORMS`` listed eleven sites and X
was not one of them, so "Publish to more" could send a piece to Telegram and
Bluesky and had no row for the third announcer. This is that row.

**Same client as polling, same cookies** (``clients/tw/client.py``): one
``upload_media`` per image, one ``create_tweet``. Nothing new is learned about
X here; the poster is the thin part. What it adds over the Posts-module branch
is the announcement shape — description, links to where the piece already
lives, hashtags — fitted to X's *weighted* 280 (``announce.tweet_length``), and
the per-piece options the Telegram panel already had: **sensitive** (follows
the rating unless set), **hashtags**, **caption**, **alt text**, and the link
picker.

⚠ **X refuses posts it thinks are automated (error 226)**, and 4.3.5 could not
prove the ``x-twitter-auth-type`` header clears it (BACKLOG ``TWAUTO``). This
poster inherits that exactly: when X refuses, ``client.last_error`` says so in
X's own words, and nothing here can force it through. Post-only, like
Telegram: a tweet cannot be edited, so ``supports_edit`` stays False and
Masterpiece sync skips X rather than failing against a live post.
"""

from __future__ import annotations

import logging
import os

import config
from posting import announce
from posting.platforms.base import PlatformPoster, PostResult, StoryUploadPackage

logger = logging.getLogger(__name__)

# X's image cap for the simple upload path. GIFs are 15 MB on X but go through
# the chunked uploader, which the client does not implement; the same 5 MB
# applies to them here and validate() says so rather than letting X 413.
_IMAGE_LIMIT = 5 * 1024 * 1024


class TwitterPoster(PlatformPoster):
    platform_id = "tw"
    platform_name = "X"
    supports_edit = False
    supports_artwork_edit = False
    supports_file_replace = False
    min_post_interval = 10
    max_file_size = _IMAGE_LIMIT
    accepted_file_types = list(announce.IMAGE_TYPES)

    def _creds(self, settings: dict | None = None) -> tuple[str, str, str]:
        """(auth_token, ct0, target_user) for THIS poster's account.

        _resolve_creds falls back to the platform's default account itself, so
        a single-account install reads the flat keys as before.
        """
        try:
            creds = self._resolve_creds("tw", settings)
        except Exception:          # no DB (fresh install) — flat keys only
            s = settings or config.get_settings()
            creds = {k: s.get(k, "") for k in ("tw_auth_token", "tw_ct0", "tw_target_user")}
        return (creds.get("tw_auth_token", ""), creds.get("tw_ct0", ""),
                creds.get("tw_target_user", ""))

    async def post(self, package: StoryUploadPackage) -> PostResult:
        _t = self._start_timer()
        auth_token, ct0, target_user = self._creds()
        if not (auth_token and ct0):
            return PostResult(success=False, duration_seconds=self._elapsed(_t),
                              error="X isn't connected for this account — Settings → X, or Browser login")

        opts = _resolve_options(package)
        is_art = bool(package.file_path
                      and package.file_type.lower() in announce.IMAGE_TYPES)
        image = package.file_path if is_art else package.thumbnail_path
        text = (announce.compose(package, is_art=is_art, with_tags=opts["tags"],
                                 limit=announce.TWEET_LIMIT, measure=announce.tweet_length)
                if opts["caption"] else "")
        if not text and not image:
            return PostResult(success=False, duration_seconds=self._elapsed(_t),
                              error="Nothing to post: no image and the caption is switched off")

        from clients.tw.client import TWClient
        client = TWClient(auth_token=auth_token, ct0=ct0, target_user=target_user)
        try:
            media_ids: list[str] = []
            if image:
                mid = await client.upload_media(image)
                if not mid:
                    return PostResult(success=False, duration_seconds=self._elapsed(_t),
                                      error=client.last_error or
                                      "X rejected the image upload (check logs)")
                media_ids.append(mid)
                alt = str((package.extra or {}).get("alt_text") or "").strip()
                if alt and opts["alt"]:
                    # Best-effort: alt text is worth having and never worth
                    # failing the post over. set_media_alt logs its own refusal.
                    await client.set_media_alt(mid, alt)
            r = await client.create_tweet(text, media_ids=media_ids or None,
                                          sensitive=opts["sensitive"])
        except Exception as e:
            logger.error("TW post failed: %s", e, exc_info=True)
            return PostResult(success=False, error=str(e), duration_seconds=self._elapsed(_t))
        finally:
            await client.close()

        if r and r.get("id"):
            return PostResult(success=True, external_id=str(r["id"]),
                              external_url=r.get("url", ""), duration_seconds=self._elapsed(_t))
        # What X actually said (4.3.4/4.3.5), never a guess.
        return PostResult(success=False, duration_seconds=self._elapsed(_t),
                          error=client.last_error or "X rejected the post and gave no reason (check logs)")

    async def edit(self, external_id: str, package: StoryUploadPackage) -> PostResult:
        return PostResult(success=False, error="X does not support editing a post")

    async def replace_file(self, external_id: str, file_path: str) -> PostResult:
        return PostResult(success=False, error="X does not support replacing a post's image")

    def validate(self, package: StoryUploadPackage) -> list[str]:
        """Refuse before anything reaches X. Length is NOT an error here: the
        caption is fitted to 280 by announce.compose (hashtags first, then the
        body), because a gallery description was never going to be a tweet and
        the per-piece text box exists for the short version."""
        errors: list[str] = []
        auth_token, ct0, _ = self._creds()
        if not (auth_token and ct0):
            errors.append("X isn't connected (Settings → X, or Browser login)")
        is_art = bool(package.file_path and package.file_type.lower() in announce.IMAGE_TYPES)
        image = package.file_path if is_art else package.thumbnail_path
        if package.file_path and not is_art:
            errors.append(f"X takes {', '.join(announce.IMAGE_TYPES)} images — not {package.file_type or 'this file'}")
        if image and not os.path.isfile(image):
            errors.append(f"File not found: {image}")
        elif image and os.path.getsize(image) > self.max_file_size:
            mb = os.path.getsize(image) / (1024 * 1024)
            errors.append(f"Image is {mb:.1f} MB — X's limit here is 5 MB")
        return errors


def _resolve_options(package: StoryUploadPackage) -> dict:
    """Per-piece options from ``categories.tw`` (artwork_reader puts them in
    ``package.extra``), each falling back to a sensible default.

    ``sensitive`` follows the rating: X requires the flag on adult media and
    an unflagged adult image is the kind of thing that gets an account
    restricted — so it is on for anything in SPOILER_RATINGS unless the piece
    says otherwise. ``tags`` is on because compose() drops the block first
    when it does not fit, so it never costs the caption its body.
    """
    x = package.extra or {}
    rating_sensitive = (package.rating or "").lower() in announce.SPOILER_RATINGS
    return {
        "sensitive": announce.flag(x.get("sensitive"), rating_sensitive),
        "tags": announce.flag(x.get("tags"), True),
        "caption": announce.flag(x.get("caption"), True),
        "alt": announce.flag(x.get("alt"), True),
    }
