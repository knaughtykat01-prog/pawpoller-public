"""Telegram Bot API client for broadcasting Posts to a channel.

PawPoller already talks to Telegram for *notifications* (polling/notifications.py,
polling/telegram.py). This client is the other direction: publishing a composed
Post from the Posts module TO a channel the bot administers — text, a single
photo + caption, or an album of up to 10 photos.

Auth is a bot token; the bot must be an admin of the target channel. The channel
is either a public ``@username`` or a numeric ``-100…`` id. Uploads go straight
through the Bot API as multipart — no public image host needed (unlike IG/Threads),
which is the nice part of using Telegram as a broadcast target.

Everything is best-effort and returns ``None`` / raises a readable error rather
than leaking a raw Telegram payload to the caller.
"""
from __future__ import annotations

import logging
import os

import httpx

logger = logging.getLogger(__name__)

API_BASE = "https://api.telegram.org"
CAPTION_LIMIT = 1024        # Telegram media-caption cap (messages allow 4096).
MESSAGE_LIMIT = 4096
HTTP_TIMEOUT = 60.0         # uploads can be slow


class TgClient:
    def __init__(self, bot_token: str, channel: str):
        self.token = (bot_token or "").strip()
        # Accept "@name", "name", "https://t.me/name", or a numeric -100… id.
        ch = (channel or "").strip()
        # A t.me/+hash (or /joinchat/) link is a PRIVATE-channel invite, not a
        # username. Splitting on "/" turned it into "@+UwCu…", a handle that
        # cannot exist, and bots cannot join by invite link at all — there is no
        # Bot API method for it. Rejecting it by name beats failing obscurely.
        if "t.me/+" in ch or "t.me/joinchat/" in ch:
            raise ValueError(
                "That's a private-channel invite link, which can't be used as a channel "
                "id — a bot can't join by invite. Use the channel's numeric -100… id "
                "instead (Settings → Telegram → Find my channel will fetch it)."
            )
        if ch.startswith("https://t.me/"):
            ch = "@" + ch.rsplit("/", 1)[-1]
        elif ch and not ch.startswith("@") and not ch.lstrip("-").isdigit():
            # ⚠ A bare word becomes a PUBLIC username, which may belong to
            # someone else. A private channel's title is not a handle — it has no
            # handle. See validate() for the case this actually caused.
            ch = "@" + ch
        self.channel = ch
        # Telegram explains its own refusals; see _ok. Kept per-instance so the
        # caller can report the REASON instead of guessing at it out loud.
        self.last_error = ""
        # Filled by validate(): WHICH chat the handle actually resolved to.
        self.resolved_chat: dict = {}

    def _url(self, method: str) -> str:
        return f"{API_BASE}/bot{self.token}/{method}"

    def _public_url(self, message_id) -> str:
        """A t.me link only exists for public @channels; numeric ids have none."""
        if self.channel.startswith("@") and message_id:
            return f"https://t.me/{self.channel[1:]}/{message_id}"
        return ""

    def _ok(self, data) -> dict | None:
        """Unwrap a Bot API response, remembering WHY a failure happened.

        Telegram always says what was wrong in ``description`` — "not enough
        rights to send text messages to the chat", "chat not found", "bot was
        blocked by the user". This threw that away and returned a bare None, so
        the only thing the caller could do was guess in the user's direction
        ("is the bot an admin with post rights?") about a question the API had
        already answered precisely. Worse, nothing logged it either, so the
        answer was not recoverable after the fact.

        ``validate()`` below has always returned the description; only the
        posting path discarded it.
        """
        if isinstance(data, dict) and data.get("ok"):
            self.last_error = ""
            return data.get("result")
        desc = data.get("description") if isinstance(data, dict) else ""
        self.last_error = desc or "Telegram rejected the request"
        logger.warning("Telegram API refused: %s", self.last_error)
        return None

    async def get_follower_count(self) -> int | None:
        """The channel's subscriber count.

        Duck-typed: ``polling/followers.capture_followers`` looks for this
        method by name and skips any client without it, so adding it here plus
        ``tg`` to ``FOLLOWER_PLATFORMS`` is the whole integration.

        This is the ONLY per-channel number the Bot API will give us. There is
        no view count (client-API only) and no forward count; reactions arrive
        as pushed updates rather than a query. See docs/specs/telegram_platform.md.
        """
        if not self.token or not self.channel:
            return None
        try:
            async with httpx.AsyncClient(timeout=20.0) as client:
                r = await client.post(self._url("getChatMemberCount"),
                                      data={"chat_id": self.channel})
                res = self._ok(r.json())
        except httpx.HTTPError as e:
            logger.warning("Telegram subscriber count failed (%s)", e)
            return None
        # _ok() returns the `result` field, which here is a bare int. A channel
        # with one subscriber is a legitimate 0-adjacent value, so only None
        # means "could not fetch".
        return res if isinstance(res, int) else None

    async def create_post(self, text: str, image_paths: list[str] | None = None,
                          spoiler: bool = False, *,
                          silent: bool = False, protect: bool = False,
                          as_document: bool = False, preview: bool = True,
                          pin: bool = False) -> dict | None:
        """Post to the channel. Returns {"id": message_id, "url": ...} or None.

        - no images → sendMessage
        - one image → sendPhoto, or sendDocument when ``as_document``
        - 2–10 images → sendMediaGroup (caption on the first)

        Options (all default to today's behaviour, so existing callers are
        unaffected):

        ``spoiler``     blur behind a tap-to-reveal (NSFW ratings).
        ``silent``      deliver without a notification ping — for bulk or minor
                        posts that shouldn't buzz every subscriber's phone.
        ``protect``     block forwarding and saving. Directly useful for an art
                        channel: it is the platform's own anti-repost control.
        ``as_document`` send the ORIGINAL FILE instead of a compressed photo.
                        Telegram re-encodes anything sent via sendPhoto, so this
                        is how full-quality art reaches the channel.
        ``preview``     link preview on text posts.
        ``pin``         pin the message after posting (a second API call; a
                        failure to pin never fails the post itself).
        """
        if not self.token or not self.channel:
            raise ValueError("Telegram bot token and channel are both required")
        image_paths = [p for p in (image_paths or []) if p and os.path.isfile(p)][:10]
        # Flags shared by every send method. Telegram wants lowercase strings in
        # multipart form data, not Python bools.
        common = {}
        if silent:
            common["disable_notification"] = "true"
        if protect:
            common["protect_content"] = "true"
        try:
            async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
                if not image_paths:
                    res = await self._send_message(client, text, common, preview)
                elif len(image_paths) == 1 and as_document:
                    res = await self._send_document(client, text, image_paths[0], common)
                elif len(image_paths) == 1:
                    res = await self._send_photo(client, text, image_paths[0], spoiler, common)
                else:
                    res = await self._send_media_group(client, text, image_paths, spoiler, common)
                if res and pin:
                    await self._pin(client, res.get("id"))
                return res
        except httpx.HTTPError as e:
            logger.warning("Telegram post failed (%s)", e)
            raise

    async def _send_message(self, client, text, common=None, preview=True) -> dict | None:
        r = await client.post(self._url("sendMessage"), data={
            "chat_id": self.channel,
            "text": (text or "")[:MESSAGE_LIMIT],
            "disable_web_page_preview": "false" if preview else "true",
            **(common or {}),
        })
        res = self._ok(r.json())
        if not res:
            return None
        mid = res.get("message_id")
        return {"id": str(mid), "url": self._public_url(mid)}

    async def _send_photo(self, client, caption, path, spoiler, common=None) -> dict | None:
        with open(path, "rb") as fh:
            data = {"chat_id": self.channel, "caption": (caption or "")[:CAPTION_LIMIT],
                    **(common or {})}
            if spoiler:
                data["has_spoiler"] = "true"
            r = await client.post(self._url("sendPhoto"), data=data,
                                  files={"photo": (os.path.basename(path), fh)})
        res = self._ok(r.json())
        if not res:
            return None
        mid = res.get("message_id")
        return {"id": str(mid), "url": self._public_url(mid)}

    async def _send_document(self, client, caption, path, common=None) -> dict | None:
        """Send the ORIGINAL file, uncompressed.

        ``sendPhoto`` re-encodes: Telegram strips the image down for fast
        delivery, which is fine for a snapshot and lossy for artwork. Sending
        the same file as a document preserves it byte-for-byte, at the cost of
        showing as an attachment rather than an inline picture.

        Worth having as an option rather than a default — most channel posts
        want the inline preview, and an artist publishing a detailed piece wants
        the pixels.
        """
        with open(path, "rb") as fh:
            data = {"chat_id": self.channel, "caption": (caption or "")[:CAPTION_LIMIT],
                    **(common or {})}
            r = await client.post(self._url("sendDocument"), data=data,
                                  files={"document": (os.path.basename(path), fh)})
        res = self._ok(r.json())
        if not res:
            return None
        mid = res.get("message_id")
        return {"id": str(mid), "url": self._public_url(mid)}

    async def _pin(self, client, message_id) -> bool:
        """Pin a message. Never raises — a post that succeeded must not be
        reported as failed because pinning was refused (the bot may lack the
        'Pin Messages' admin right, which is separate from 'Post Messages')."""
        if not message_id:
            return False
        try:
            r = await client.post(self._url("pinChatMessage"),
                                  data={"chat_id": self.channel,
                                        "message_id": str(message_id),
                                        "disable_notification": "true"})
            if self._ok(r.json()) is not None:
                return True
            logger.warning("Telegram: posted but could not pin (%s)", self.last_error)
        except httpx.HTTPError as e:
            logger.warning("Telegram: posted but could not pin (%s)", e)
        return False

    async def _send_media_group(self, client, caption, paths, spoiler, common=None) -> dict | None:
        import json as _json
        media, files = [], {}
        for i, p in enumerate(paths):
            key = f"file{i}"
            item = {"type": "photo", "media": f"attach://{key}"}
            if i == 0 and caption:
                item["caption"] = caption[:CAPTION_LIMIT]
            if spoiler:
                item["has_spoiler"] = True
            media.append(item)
            files[key] = (os.path.basename(p), open(p, "rb"))
        try:
            r = await client.post(self._url("sendMediaGroup"),
                                  data={"chat_id": self.channel,
                                        "media": _json.dumps(media),
                                        **(common or {})},
                                  files=files)
        finally:
            for _, fh in files.values():
                try:
                    fh.close()
                except Exception:
                    pass
        res = self._ok(r.json())
        if not res:
            return None
        # sendMediaGroup returns a list of messages; key off the first.
        first = res[0] if isinstance(res, list) and res else {}
        mid = first.get("message_id")
        return {"id": str(mid), "url": self._public_url(mid)}

    async def validate(self) -> str | None:
        """Confirm the token + channel work: getChat on the channel. Returns an
        error string, or None on success. Used by the connect/test flow.

        On success the resolved chat is kept on ``self.resolved_chat`` so the
        caller can say WHICH channel it reached.

        ⚠ Succeeding here does NOT mean you found the user's channel. A bare
        name typed by the user is prefixed to ``@name`` by __init__, and a public
        channel owned by a stranger may already hold that username — getChat
        reads any public channel, so it returns a confident success for entirely
        the wrong chat. Observed live: a user's PRIVATE channel titled "Testing"
        sent us to ``@testing``, an unrelated public channel, which validated
        cleanly and then refused the post. A private channel has no username at
        all and is reachable only by its numeric -100… id, so the title a user
        reads off their screen is never a valid handle.
        """
        if not self.token:
            return "No bot token"
        if not self.channel:
            return "No channel"
        try:
            async with httpx.AsyncClient(timeout=20.0) as client:
                r = await client.post(self._url("getChat"),
                                      data={"chat_id": self.channel})
                data = r.json()
                if data.get("ok"):
                    self.resolved_chat = data.get("result") or {}
                    return None
                return data.get("description") or "Telegram rejected the channel"
        except httpx.HTTPError as e:
            return f"Could not reach Telegram ({e})"
