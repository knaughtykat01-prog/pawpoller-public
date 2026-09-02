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

    async def create_post(self, text: str, image_paths: list[str] | None = None,
                          spoiler: bool = False) -> dict | None:
        """Post to the channel. Returns {"id": message_id, "url": ...} or None.

        - no images → sendMessage
        - one image → sendPhoto (caption)
        - 2–10 images → sendMediaGroup (caption on the first)
        ``spoiler`` blurs the photo(s) behind a tap-to-reveal (for NSFW ratings).
        """
        if not self.token or not self.channel:
            raise ValueError("Telegram bot token and channel are both required")
        image_paths = [p for p in (image_paths or []) if p and os.path.isfile(p)][:10]
        try:
            async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
                if not image_paths:
                    return await self._send_message(client, text)
                if len(image_paths) == 1:
                    return await self._send_photo(client, text, image_paths[0], spoiler)
                return await self._send_media_group(client, text, image_paths, spoiler)
        except httpx.HTTPError as e:
            logger.warning("Telegram post failed (%s)", e)
            raise

    async def _send_message(self, client, text) -> dict | None:
        r = await client.post(self._url("sendMessage"), data={
            "chat_id": self.channel,
            "text": (text or "")[:MESSAGE_LIMIT],
            "disable_web_page_preview": "false",
        })
        res = self._ok(r.json())
        if not res:
            return None
        mid = res.get("message_id")
        return {"id": str(mid), "url": self._public_url(mid)}

    async def _send_photo(self, client, caption, path, spoiler) -> dict | None:
        with open(path, "rb") as fh:
            data = {"chat_id": self.channel, "caption": (caption or "")[:CAPTION_LIMIT]}
            if spoiler:
                data["has_spoiler"] = "true"
            r = await client.post(self._url("sendPhoto"), data=data,
                                  files={"photo": (os.path.basename(path), fh)})
        res = self._ok(r.json())
        if not res:
            return None
        mid = res.get("message_id")
        return {"id": str(mid), "url": self._public_url(mid)}

    async def _send_media_group(self, client, caption, paths, spoiler) -> dict | None:
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
                                        "media": _json.dumps(media)},
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
