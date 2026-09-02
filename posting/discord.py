"""Discord announce webhook (gap G4).

Post a compact embed to a user-configured Discord webhook when new work is
published — the place furry audiences actually gather but PawPoller couldn't
reach. A webhook URL grants posting to one channel (no OAuth); it lives in
settings and is used only to POST embeds.

Opt-in on two levels: nothing is sent unless a webhook URL is set, and
auto-announce only fires when `discord_announce_on_publish` is on. The manual
"Announce to Discord" action sends as long as a URL is configured. Announcing
must never break a publish, so the auto hook swallows every error.

Adult content: for mature/adult/explicit ratings the auto hook drops the image
thumbnail so an explicit picture is never pushed into a channel unexpectedly —
the link still goes through, just without the inline preview.
"""
from __future__ import annotations

import logging
from typing import Any

import httpx

import config

logger = logging.getLogger(__name__)

# Embed accent per rating so an adult announce reads distinctly from a SFW one.
_RATING_COLOR = {
    "general": 0x4CAF50, "mature": 0xFF9800,
    "adult": 0xE53935, "explicit": 0xE53935,
}
_BLURPLE = 0x5865F2
_ADULT = {"mature", "adult", "explicit"}


def _webhook_url(settings: dict | None = None) -> str:
    s = settings or config.get_settings()
    return (s.get("discord_webhook_url") or "").strip()


def is_configured(settings: dict | None = None) -> bool:
    return bool(_webhook_url(settings))


def build_embed(kind: str, title: str, url: str | None = None,
                thumbnail: str | None = None, rating: str | None = None,
                platforms: list[str] | None = None) -> dict:
    """Build a Discord embed dict for a published piece."""
    label = {"post": "📣 New post", "artwork": "🎨 New artwork",
             "story": "📖 New story"}.get(kind, "New")
    embed: dict[str, Any] = {
        "title": (title or label)[:250],
        "color": _RATING_COLOR.get((rating or "").lower(), _BLURPLE),
        "footer": {"text": f"PawPoller · {label}"},
    }
    if url:
        embed["url"] = url
    fields = []
    if platforms:
        fields.append({"name": "Where", "value": ", ".join(platforms)[:1000], "inline": True})
    if rating:
        fields.append({"name": "Rating", "value": rating, "inline": True})
    if fields:
        embed["fields"] = fields
    # Only ever attach a public http(s) URL Discord can fetch — never a local path.
    if thumbnail and thumbnail.startswith(("http://", "https://")):
        embed["thumbnail"] = {"url": thumbnail}
    return embed


async def _send(webhook_url: str, embed: dict, content: str = "") -> bool:
    """POST an embed to a Discord webhook. Returns True on 2xx."""
    payload: dict[str, Any] = {"embeds": [embed]}
    if content:
        payload["content"] = content[:1900]
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(webhook_url, json=payload)
        if resp.status_code >= 300:
            logger.warning("Discord webhook returned %s: %s", resp.status_code, resp.text[:200])
            return False
        return True
    except Exception as e:  # noqa: BLE001 — network failure must not propagate
        logger.warning("Discord webhook post failed: %s", e)
        return False


async def announce(kind: str, title: str, **kw) -> bool:
    """Manual announce — sends as long as a webhook is configured, regardless of
    the auto toggle. Returns True on success, False if unconfigured / failed."""
    url = _webhook_url()
    if not url:
        return False
    return await _send(url, build_embed(kind, title, **kw))


async def announce_publish(kind: str, title: str, **kw) -> None:
    """Auto-announce hook, awaited by the publishers after a successful publish.
    No-op unless a webhook is set AND announce-on-publish is enabled. Never
    raises — announcing must never break a publish."""
    try:
        s = config.get_settings()
        if not is_configured(s) or not s.get("discord_announce_on_publish", False):
            return
        if (kw.get("rating") or "").lower() in _ADULT:
            kw.pop("thumbnail", None)   # don't push an explicit preview into a channel
        await _send(_webhook_url(s), build_embed(kind, title, **kw))
    except Exception as e:  # noqa: BLE001
        logger.debug("announce_publish skipped: %s", e)
