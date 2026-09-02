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
        if ch.startswith("https://t.me/"):
            ch = "@" + ch.rsplit("/", 1)[-1]
        elif ch and not ch.startswith("@") and not ch.lstrip("-").isdigit():
            ch = "@" + ch
        self.channel = ch

    def _url(self, method: str) -> str:
        return f"{API_BASE}/bot{self.token}/{method}"

    def _public_url(self, message_id) -> str:
        """A t.me link only exists for public @channels; numeric ids have none."""
        if self.channel.startswith("@") and message_id:
            return f"https://t.me/{self.channel[1:]}/{message_id}"
        return ""

    @staticmethod
    def _ok(data) -> dict | None:
        if isinstance(data, dict) and data.get("ok"):
            return data.get("result")
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
        error string, or None on success. Used by the connect/test flow."""
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
                    return None
                return data.get("description") or "Telegram rejected the channel"
        except httpx.HTTPError as e:
            return f"Could not reach Telegram ({e})"
