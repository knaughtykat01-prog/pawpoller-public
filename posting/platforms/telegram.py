"""Telegram broadcast poster — artwork and story announcements.

Makes Telegram a first-class publish target. Until now it could only be reached
from the Posts / microblog module (2.198.0), which left the odd situation that
PawPoller could publish a piece to nine sites and had no way to tell the
channel about it.

**Telegram is the cheapest image poster in this codebase.** Every other one
fights its platform's upload model — ``instagram.py`` spends half its ``post()``
resolving a public image host (stash, uuid token, 15-minute TTL, desktop→server
relay) because Meta never accepts bytes. Telegram takes the file directly as
multipart. There is no host to configure and nothing to clean up.

Two content types, one implementation
-------------------------------------
Following ``bluesky.py``, which solved this first: an **artwork** post sends the
art itself, a **story** post sends its cover thumbnail with an announcement.
The discriminator is the package's file type, not a separate poster.

⚠ A story body is never sent. Telegram caps a message at 4,096 characters —
about 700 words against stories that run to 70,000. A channel is an
announcement feed, not an archive, so a story post is a blurb plus links to
where the work actually lives.

Post-only, deliberately
-----------------------
⚠ Telegram *does* have ``editMessageCaption`` / ``editMessageMedia``, so
``supports_edit = True`` looks correct — and would be a bug. The Bot API
refuses to edit any message **older than 48 hours**. Declaring edit support
would make Masterpiece "Sync to sites" attempt an edit on a two-week-old post,
fail, and record ``status='failed'`` against a live, correctly-published
message. That is exactly the failure ``supports_artwork_edit`` was introduced to
prevent for DeviantArt. Post-only is the honest declaration until an age check
exists to go with it.

See ``docs/specs/telegram_broadcast.md``.
"""
from __future__ import annotations

import logging
import os

import config
from posting.platforms.base import PlatformPoster, PostResult, StoryUploadPackage

logger = logging.getLogger(__name__)

# Telegram's own limits. A media caption is a QUARTER of a plain message, which
# is the one that bites: an artwork description comfortably clears 1,024.
CAPTION_LIMIT = 1024
MESSAGE_LIMIT = 4096

_IMAGE_TYPES = ("png", "jpg", "jpeg", "gif", "webp")

# Ratings that get a tap-to-reveal blur. Mirrors the vocabulary bluesky.py maps
# to its own labels, so one rating field drives both platforms consistently.
_SPOILER_RATINGS = ("adult", "explicit", "nsfw", "mature", "questionable")


class TelegramPoster(PlatformPoster):

    platform_id = "tg"
    platform_name = "Telegram"
    # ⚠ See the module docstring: NOT because the API lacks an edit, but because
    # its edit expires after 48h and a failed edit poisons a healthy post.
    supports_edit = False
    supports_file_replace = False
    # Telegram allows ~20 messages/minute to one channel. 5s keeps a bulk sync
    # comfortably inside that without needing a backoff of its own.
    min_post_interval = 5
    # Bot API photo cap is 10 MB. Unlike Instagram we do NOT pre-downscale, so
    # this is enforced in validate() rather than silently absorbed.
    max_file_size = 10 * 1024 * 1024
    accepted_file_types = list(_IMAGE_TYPES)
    # api.telegram.org is reachable from the VM. Overriding this to "desktop"
    # would strand jobs in the queue — see the base-class warning about FA.
    requires_mode = "any"

    async def post(self, package: StoryUploadPackage) -> PostResult:
        """Broadcast one artwork, or one story announcement, to the channel."""
        _t = self._start_timer()
        try:
            creds = self._resolve_creds("tg", config.get_settings())
            settings = config.get_settings()
            # The posting bot falls back to the notification bot, matching
            # post_publisher's tg branch so both paths resolve identically.
            token = creds.get("tg_bot_token", "") or settings.get("telegram_bot_token", "")
            channel = creds.get("tg_channel", "") or settings.get("tg_channel", "")
            if not token:
                return PostResult(success=False, duration_seconds=self._elapsed(_t),
                                  error="Telegram bot token isn't set (Settings → Telegram)")
            if not channel:
                return PostResult(success=False, duration_seconds=self._elapsed(_t),
                                  error="No Telegram channel set (Settings → Telegram)")

            from clients.tg.client import TgClient
            try:
                client = TgClient(bot_token=token, channel=channel)
            except ValueError as e:
                # The normaliser refuses invite links by name — surface that
                # rather than letting it read as a generic posting failure.
                return PostResult(success=False, error=str(e),
                                  duration_seconds=self._elapsed(_t))

            is_art = bool(package.file_path
                          and package.file_type.lower() in _IMAGE_TYPES)
            image = package.file_path if is_art else package.thumbnail_path
            opts = _resolve_options(package, settings)
            text = _build_caption(package, has_image=bool(image), is_art=is_art,
                                  with_tags=opts["tags"]) if opts["caption"] else ""

            images = [image] if image and os.path.isfile(image) else []
            result = await client.create_post(
                text, image_paths=images, spoiler=opts["spoiler"],
                silent=opts["silent"], protect=opts["protect"],
                # Sending as a document only makes sense with a real image;
                # a text announcement has no file whose quality to preserve.
                as_document=opts["document"] and bool(images),
                preview=opts["preview"], pin=opts["pin"])
            if not result:
                # client._ok() keeps Telegram's own description; a bare "failed"
                # is what sent a user chasing admin rights that were already
                # correct (4.0.3).
                reason = getattr(client, "last_error", "") or "Telegram refused the post"
                return PostResult(success=False, error=reason,
                                  duration_seconds=self._elapsed(_t))

            return PostResult(success=True,
                              external_id=str(result.get("id", "")),
                              external_url=result.get("url", ""),
                              duration_seconds=self._elapsed(_t))
        except Exception as e:
            logger.exception("Telegram post failed")
            return PostResult(success=False, error=str(e),
                              duration_seconds=self._elapsed(_t))

    async def edit(self, external_id: str, package: StoryUploadPackage) -> PostResult:
        return PostResult(success=False, error=(
            "Telegram posts can't be edited by PawPoller — the Bot API only allows "
            "edits within 48 hours of posting, so an edit is not reliably available"))

    async def replace_file(self, external_id: str, file_path: str) -> PostResult:
        return PostResult(success=False, error=(
            "Telegram doesn't support replacing a post's media after 48 hours"))

    def validate(self, package: StoryUploadPackage) -> list[str]:
        """Fail (and warn) before anything is broadcast to real subscribers."""
        errors: list[str] = []
        s = config.get_settings()
        if not (s.get("tg_bot_token", "") or s.get("telegram_bot_token", "")):
            errors.append("Telegram bot token isn't set (Settings → Telegram)")
        if not s.get("tg_channel", ""):
            errors.append("No Telegram channel set (Settings → Telegram)")

        is_art = bool(package.file_path and package.file_type.lower() in _IMAGE_TYPES)
        image = package.file_path if is_art else package.thumbnail_path
        if image and not os.path.isfile(image):
            errors.append(f"File not found: {image}")
        elif image and os.path.getsize(image) > self.max_file_size:
            mb = os.path.getsize(image) / (1024 * 1024)
            errors.append(f"Image is {mb:.1f} MB — Telegram's limit is 10 MB")

        # A post with neither text nor image would be an empty broadcast.
        if not image and not _build_caption(package, has_image=False, is_art=is_art).strip():
            errors.append("Nothing to post — no image and no text")

        # ⚠ Warn rather than truncate silently. The caption cap is 1,024 and an
        # artwork description clears it easily; slicing without a word is how a
        # broadcast goes out mangled to real subscribers.
        limit = CAPTION_LIMIT if image else MESSAGE_LIMIT
        body = _build_caption(package, has_image=bool(image), is_art=is_art)
        if len(body) > limit:
            errors.append(
                f"Text is {len(body)} characters — Telegram's limit "
                f"{'for a photo caption' if image else 'for a message'} is {limit}. "
                "Shorten the description or post without the image.")
        return errors


def _flag(value, default: bool) -> bool:
    """Read a tri-state option: unset falls back, anything else is coerced.

    Per-artwork options arrive as JSON, so a value may be a real bool or one of
    the strings a human typed into art.json. Bare bool() treats "false" as TRUE,
    which would silently invert the setting — and for `protect` or `spoiler`
    that is the wrong way round on a live broadcast.
    """
    if value is None:
        return default
    if isinstance(value, str):
        return value.strip().lower() not in ("", "0", "false", "no", "off")
    return bool(value)


def _resolve_options(package: StoryUploadPackage, settings: dict) -> dict:
    """Channel-wide defaults from Settings, overridden per artwork.

    Per-artwork overrides ride in ``package.extra``, which artwork_reader fills
    from ``categories_by_platform['tg']`` — the field that already exists for
    "this platform's submission parameters". So no new plumbing was needed: an
    art.json can carry

        "categories": {"tg": {"spoiler": true, "tags": false, "document": true}}

    and it reaches here untouched.
    """
    x = package.extra or {}
    rating_spoiler = (package.rating or "").lower() in _SPOILER_RATINGS
    return {
        # Rating decides the blur unless the artwork overrides it, so a
        # general-rated piece can still be hidden and an adult one shown.
        "spoiler": _flag(x.get("spoiler"), rating_spoiler),
        # Hashtags are appended by default; a channel with its own conventions
        # can drop them globally (tg_no_tags) or on one piece.
        "tags": _flag(x.get("tags"), not _flag(settings.get("tg_no_tags"), False)),
        # A caption-less post is a legitimate choice for a pure-image channel.
        "caption": _flag(x.get("caption"), True),
        "silent": _flag(x.get("silent"), _flag(settings.get("tg_silent"), False)),
        "protect": _flag(x.get("protect"), _flag(settings.get("tg_protect"), False)),
        "document": _flag(x.get("document"), _flag(settings.get("tg_document"), False)),
        "pin": _flag(x.get("pin"), False),
        "preview": _flag(x.get("preview"), True),
    }



def _build_caption(package: StoryUploadPackage, *, has_image: bool, is_art: bool,
                   with_tags: bool = True) -> str:
    """Artwork caption, or a story announcement.

    Artwork: description (or title) + hashtags — the same shape instagram.py
    uses, minus its 30-hashtag cap, which is an Instagram rule and not a
    Telegram one.

    Story: title + blurb + hashtags. The BODY IS NEVER INCLUDED — see the module
    docstring. Where the story is actually published is carried by the caller in
    ``extra['links']`` when it has them; a story announcement with nowhere to
    point is still a valid "this exists" post.
    """
    parts: list[str] = []

    if is_art:
        body = (package.description or package.title or "").strip()
        if body:
            parts.append(body)
    else:
        title = (package.chapter_title or package.title or package.story_name or "").strip()
        if title:
            parts.append(title)
        blurb = (package.description or "").strip()
        if blurb:
            parts.append(blurb)
        links = (package.extra or {}).get("links") or []
        if links:
            parts.append("\n".join(str(u) for u in links if u))

    if with_tags:
        tags = _hashtags(package.tags)
        if tags:
            parts.append(tags)
    return "\n\n".join(p for p in parts if p)


def _hashtags(tags: list[str]) -> str:
    """Tags as Telegram hashtags — alnum/underscore, deduped, order preserved.

    ⚠ No 30-tag cap. That is Instagram's rule; copying it here would silently
    drop tags for no reason. The length ceiling is the caption limit, which
    validate() reports rather than enforcing by truncation.
    """
    seen: set[str] = set()
    out: list[str] = []
    for t in tags or []:
        h = "".join(ch for ch in str(t) if ch.isalnum() or ch == "_")
        if not h or h.lower() in seen:
            continue
        seen.add(h.lower())
        out.append("#" + h)
    return " ".join(out)
